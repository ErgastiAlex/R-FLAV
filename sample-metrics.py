import torch
# the first flag below was False when we tested this script but True makes A100 training a lot faster:
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

import numpy as np
import time
import logging
import os

from diffusers.models import AutoencoderKL
from efficientvit.ae_model_zoo import DCAE_HF
from models import FLAV_models

from diffusion.rectified_flow import RectifiedFlow

from diffusers.training_utils import EMAModel
from accelerate import Accelerator
from accelerate.utils import ProjectConfiguration, set_seed
from converter import Generator

import einops
from common_parser import CommonParser
from utils import *

AUDIO_T_PER_FRAME = 1600 // 160 

#################################################################################
#                                  Sampling Loop                                #
#################################################################################

def main(args):
    assert torch.cuda.is_available(), "Sampling currently requires at least one GPU."

    os.makedirs(args.experiment_dir, exist_ok=True)  # Make results folder (holds all experiment subfolders)
    model_string_name = args.model.replace("/", "-") 
    experiment_dir = f"{args.experiment_dir}/{model_string_name}"  # Create an experiment folder
    checkpoint_dir = f"{experiment_dir}/checkpoints"  # Loading saved model checkpoints
    
    os.makedirs(os.path.join(experiment_dir, args.results_dir), exist_ok=True)

    device = "cuda"

    set_seed(args.seed)  # Set global seed for reproducibility

    if args.use_sd_vae:
        vae = AutoencoderKL.from_pretrained(f"stabilityai/sd-vae-ft-{args.vae}")
    else:
        vae = DCAE_HF.from_pretrained(f"mit-han-lab/dc-ae-f32c32-sana-1.0")

    assert args.image_size % 8 == 0, "Image size must be divisible by 8 (for the VAE encoder)."
    latent_size = args.image_size // (8 if args.use_sd_vae else 32)
    
    model = FLAV_models[args.model](
        latent_size=latent_size,
        in_channels = (4 if args.use_sd_vae else 32),
        num_classes=args.num_classes,
        predict_frames = args.predict_frames,
        grad_ckpt = args.grad_ckpt,
        causal_attn = args.causal_attn,
    )

    if args.num_classes > 0:
        loader = get_dataloader(args, None, args.video_length, train=True)

    state_dict = torch.load(os.path.join(checkpoint_dir, "ema.pth"))

    print(model)
    ema = EMAModel(model.parameters())
    ema.load_state_dict(state_dict)
    ema.copy_to(model.parameters())

    rectified_flow = RectifiedFlow(num_timesteps=args.num_timesteps, 
                                   warmup_timesteps=args.predict_frames, 
                                   window_size=args.predict_frames)

    vocoder = Generator.from_pretrained(args.vocoder_ckpt)

    model.to(device)    
    vocoder.to(device)
    vae.to(device)

    os.makedirs(os.path.join(experiment_dir,args.results_dir, "samples/"), exist_ok=True)
    os.makedirs(os.path.join(experiment_dir,args.results_dir, "samples/audio"), exist_ok=True)
    os.makedirs(os.path.join(experiment_dir,args.results_dir, "samples/video"), exist_ok=True)

    video_latent_size = (args.batch_size, args.predict_frames, 4 if args.use_sd_vae else 32, latent_size, latent_size)
    audio_latent_size = (args.batch_size, args.predict_frames, 1, 256, AUDIO_T_PER_FRAME)
    
    classes = torch.arange(args.num_classes).to(device)
    
    if args.num_classes > 0:
        loader = iter(loader)
    for i in range(args.num_videos//args.batch_size):
        if args.num_classes > 0:
            _, _, y = next(loader)
            y = y.to(device)

        # start_time = time.time()
        video, audio = generate_sample(vae = vae, 
            rectified_flow = rectified_flow, 
            forward_fn = model.forward_with_cfg if args.num_classes > 0 else model.forward, 
            video_length = args.video_length, 
            video_latent_size = video_latent_size,
            audio_latent_size = audio_latent_size,
            y = y if args.num_classes > 0 else None,
            cfg_scale = args.cfg_scale if args.num_classes > 0 else None,
            device = device)
        
        # print(f"Time taken: {time.time()-start_time}", flush=True)

        # Save fake videos:
        wavs = get_wavs(audio, vocoder, args.audio_scale, device)
        for j,(vid, wav) in enumerate(zip(video, wavs)):
            save_multimodal(vid, wav, os.path.join(experiment_dir,args.results_dir, "samples/"), f"sample_{i*(args.batch_size)+j}")



if __name__ == "__main__":
    # Default args here will train DiT-XL/2 with the hyperparameters we used in our paper (except training iters).
    parser = CommonParser().get_parser()
    parser.add_argument("--model-ckpt", type=str)
    parser.add_argument("--grad-ckpt", action="store_true")
    parser.add_argument("--cfg-scale", type=float, default=1)
    parser.add_argument("--num-videos", type=int, default=2048)
    
    args = parser.parse_args()
    main(args)
