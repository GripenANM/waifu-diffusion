import PIL
import gradio as gr
import argparse, os, sys, glob
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange, repeat
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
import torch.nn as nn
from contextlib import contextmanager, nullcontext

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler

from k_diffusion.sampling import sample_lms
from k_diffusion.external import CompVisDenoiser

parser = argparse.ArgumentParser()

parser.add_argument(
    "--outdir",
    type=str,
    nargs="?",
    help="dir to write results to",
    default="outputs/img2img-samples"
)

parser.add_argument(
    "--skip_grid",
    action='store_true',
    help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
)

parser.add_argument(
    "--skip_save",
    action='store_true',
    help="do not save indiviual samples. For speed measurements.",
)
parser.add_argument(
    "--C",
    type=int,
    default=4,
    help="latent channels",
)
parser.add_argument(
    "--f",
    type=int,
    default=8,
    help="downsampling factor, most often 8 or 16",
)
parser.add_argument(
    "--n_rows",
    type=int,
    default=0,
    help="rows in the grid (default: n_samples)",
)
parser.add_argument(
    "--from-file",
    type=str,
    help="if specified, load prompts from this file",
)
parser.add_argument(
    "--config",
    type=str,
    default="configs/stable-diffusion/v1-inference.yaml",
    help="path to config which constructs model",
)
parser.add_argument(
    "--ckpt",
    type=str,
    default="models/ldm/stable-diffusion-v1/model.ckpt",
    help="path to checkpoint of model",
)
parser.add_argument(
    "--precision",
    type=str,
    help="evaluate at this precision",
    choices=["full", "autocast"],
    default="autocast"
)

opt = parser.parse_args()

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cuda")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.to('cuda')
    model.eval()
    return model

def load_img_pil(img_pil):
    image = img_pil.convert("RGB")
    w, h = image.size
    print(f"loaded input image of size ({w}, {h})")
    w, h = map(lambda x: x - x % 64, (w, h))  # resize to integer multiple of 64
    image = image.resize((w, h), resample=PIL.Image.LANCZOS)
    print(f"cropped image to size ({w}, {h})")
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.

def load_img(path):
    return load_img_pil(Image.open(path))

class CFGDenoiser(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.inner_model = model

    def forward(self, x, sigma, uncond, cond, cond_scale):
        x_in = torch.cat([x] * 2)
        sigma_in = torch.cat([sigma] * 2)
        cond_in = torch.cat([uncond, cond])
        uncond, cond = self.inner_model(x_in, sigma_in, cond=cond_in).chunk(2)
        return uncond + (cond - uncond) * cond_scale

config = OmegaConf.load("configs/stable-diffusion/v1-inference.yaml")
model = load_model_from_config(config, "models/ldm/stable-diffusion-v1/model.ckpt")

device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
model = model.half().to(device)

def reshape_c_uc(c, uc):
    # I have no idea how to generate an empty tensor that's valid for the model,
    # so I'm gonna just pass in an empty prompt and hope it works!
    padding = model.get_learned_conditioning(["" for _ in range(c.shape[0])])
    while c.shape[1] != uc.shape[1]:
        if c.shape[1] > uc.shape[1]:
            uc = torch.cat([uc, padding], dim=1)
        else:
            c = torch.cat([c, padding], dim=1)
    return c, uc

def dream(prompt: str, ddim_steps: int, sampler: str, fixed_code: bool, ddim_eta: float, n_iter: int, n_samples: int, cfg_scale: float, seed: int, height: int, width: int):
    torch.cuda.empty_cache()

    opt.H = height
    opt.W = width

    rng_seed = seed_everything(seed)

    if sampler == 'plms':
        sampler = PLMSSampler(model)
    if sampler == 'ddim':
        sampler = DDIMSampler(model)
    if sampler == 'k_lms':
        model_wrap = CompVisDenoiser(model)

    opt.outdir = "outputs/txt2img-samples"

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    batch_size = n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    start_code = None
    if fixed_code:
        start_code = torch.randn([n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

    precision_scope = autocast if opt.precision=="autocast" else nullcontext
    output_images = []
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                for n in trange(n_iter, desc="Sampling"):
                    for prompts in tqdm(data, desc="data"):
                        uc = None
                        if cfg_scale != 1.0:
                            uc = model.get_learned_conditioning(batch_size * [""])
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)
                        c = model.get_learned_conditioning(prompts)
                        shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                        if uc is not None:
                            c, uc = reshape_c_uc(c, uc)
                        if sampler == 'k_lms':
                            sigmas = model_wrap.get_sigmas(ddim_steps)
                            model_wrap_cfg = CFGDenoiser(model_wrap)
                            x = torch.randn([n_samples, *shape], device=device) * sigmas[0]
                            extra_args = {'cond': c, 'uncond': uc, 'cond_scale': cfg_scale}
                            samples_ddim = sample_lms(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False)
                        else:
                            samples_ddim, _ = sampler.sample(S=ddim_steps,
                                                            conditioning=c,
                                                            batch_size=n_samples,
                                                            shape=shape,
                                                            verbose=False,
                                                            unconditional_guidance_scale=cfg_scale,
                                                            unconditional_conditioning=uc,
                                                            eta=ddim_eta,
                                                            x_T=start_code)

                        x_samples_ddim = model.decode_first_stage(samples_ddim)
                        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)

                        if not opt.skip_save:
                            for x_sample in x_samples_ddim:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                Image.fromarray(x_sample.astype(np.uint8)).save(
                                    os.path.join(sample_path, f"{base_count:05}-{rng_seed}_{prompt.replace(' ', '_')[:128]}.png"))
                                output_images.append(Image.fromarray(x_sample.astype(np.uint8)))
                                base_count += 1

                        if not opt.skip_grid:
                            all_samples.append(x_samples_ddim)

                if not opt.skip_grid:
                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    Image.fromarray(grid.astype(np.uint8)).save(os.path.join(outpath, f'grid-{grid_count:04}.png'))
                    grid_count += 1

                toc = time.time()
    del sampler
    return output_images, rng_seed

def translation(prompt: str, init_img, ddim_steps: int, ddim_eta: float, n_iter: int, n_samples: int, cfg_scale: float, denoising_strength: float, seed: int, height: int, width: int):
    torch.cuda.empty_cache()
    rng_seed = seed_everything(seed)

    sampler = DDIMSampler(model)

    opt.outdir = "outputs/img2img-samples"

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    batch_size = n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        prompt = prompt
        assert prompt is not None
        data = [batch_size * [prompt]]
    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    image = init_img.convert("RGB")
    w, h = image.size
    print(f"loaded input image of size ({w}, {h})")
    w, h = map(lambda x: x - x % 32, (width, height))  # resize to integer multiple of 32
    image = image.resize((w, h), resample=PIL.Image.LANCZOS)
    print(f"cropped image to size ({w}, {h})")
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)

    output_images = []
    precision_scope = autocast if opt.precision == "autocast" else nullcontext
    with torch.no_grad():
        with precision_scope("cuda"):
            init_image = 2.*image - 1.
            init_image = init_image.to(device)
            init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
            init_latent = model.get_first_stage_encoding(model.encode_first_stage(init_image))  # move to latent space

            sampler.make_schedule(ddim_num_steps=ddim_steps, ddim_eta=ddim_eta, verbose=False)

            assert 0. <= denoising_strength <= 1., 'can only work with strength in [0.0, 1.0]'
            t_enc = int(denoising_strength * ddim_steps)
            print(f"target t_enc is {t_enc} steps")
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                for n in trange(n_iter, desc="Sampling"):
                    for prompts in tqdm(data, desc="data"):
                        uc = None
                        if cfg_scale != 1.0:
                            uc = model.get_learned_conditioning(batch_size * [""])
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)
                        c = model.get_learned_conditioning(prompts)

                        # encode (scaled latent)
                        z_enc = sampler.stochastic_encode(init_latent, torch.tensor([t_enc]*batch_size).to(device))
                        # decode it
                        samples = sampler.decode(z_enc, c, t_enc, unconditional_guidance_scale=cfg_scale,
                                                 unconditional_conditioning=uc,)

                        x_samples = model.decode_first_stage(samples)
                        x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                        if not opt.skip_save:
                            for x_sample in x_samples:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                Image.fromarray(x_sample.astype(np.uint8)).save(
                                    os.path.join(sample_path, f"{base_count:05}-{rng_seed}_{prompt.replace(' ', '_')[:128]}.png"))
                                output_images.append(Image.fromarray(x_sample.astype(np.uint8)))
                                base_count += 1
                        all_samples.append(x_samples)

                if not opt.skip_grid:
                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    Image.fromarray(grid.astype(np.uint8)).save(os.path.join(outpath, f'grid-{grid_count:04}.png'))
                    Image.fromarray(grid.astype(np.uint8))
                    grid_count += 1

                toc = time.time()
    del sampler
    return output_images, rng_seed

dream_interface = gr.Interface(
    dream,
    inputs=[
        gr.Textbox(placeholder="A corgi wearing a top hat as an oil painting.", lines=1),
        gr.Slider(minimum=1, maximum=150, step=1, label="Sampling Steps", value=50),
        gr.Dropdown(choices=['plms', 'ddim', 'k_lms'], value='k_lms', label='Sampler'),
        gr.Checkbox(label='Enable Fixed Code sampling', value=False),
        gr.Slider(minimum=0.0, maximum=1.0, step=0.01, label="DDIM ETA", value=0.0, visible=False),
        gr.Slider(minimum=1, maximum=8, step=1, label='Sampling iterations', value=2),
        gr.Slider(minimum=1, maximum=8, step=1, label='Samples per iteration', value=2),
        gr.Slider(minimum=1.0, maximum=20.0, step=0.5, label='Classifier Free Guidance Scale', value=7.0),
        gr.Number(label='Seed', value=-1),
        gr.Slider(minimum=64, maximum=2048, step=64, label="Height", value=512),
        gr.Slider(minimum=64, maximum=2048, step=64, label="Width", value=512),
    ],
    outputs=[
        gr.Gallery(),
        gr.Number(label='Seed')
    ],
    title="Stable Diffusion Text-to-Image",
    description="Generate images from text with Stable Diffusion",
)

img2img_interface = gr.Interface(
    translation,
    inputs=[
        gr.Textbox(placeholder="A fantasy landscape, trending on artstation.", lines=1),
        gr.Image(value="https://raw.githubusercontent.com/CompVis/stable-diffusion/main/assets/stable-samples/img2img/sketch-mountains-input.jpg", source="upload", interactive=True, type="pil"),
        gr.Slider(minimum=1, maximum=150, step=1, label="Sampling Steps", value=50),
        gr.Slider(minimum=0.0, maximum=1.0, step=0.01, label="DDIM ETA", value=0.0, visible=False),
        gr.Slider(minimum=1, maximum=8, step=1, label='Sampling iterations', value=2),
        gr.Slider(minimum=1, maximum=8, step=1, label='Samples per iteration', value=2),
        gr.Slider(minimum=1.0, maximum=20.0, step=0.5, label='Classifier Free Guidance Scale', value=7.0),
        gr.Slider(minimum=0.0, maximum=1.0, step=0.01, label='Denoising Strength', value=0.75),
        gr.Number(label='Seed', value=-1),
        gr.Slider(minimum=64, maximum=2048, step=64, label="Resize Height", value=512),
        gr.Slider(minimum=64, maximum=2048, step=64, label="Resize Width", value=512),
    ],
    outputs=[
        gr.Gallery(),
        gr.Number(label='Seed')
    ],
    title="Stable Diffusion Image-to-Image",
    description="Generate images from images with Stable Diffusion",
)

demo = gr.TabbedInterface(interface_list=[dream_interface, img2img_interface], tab_names=["Dream", "Image Translation"])

demo.launch()
