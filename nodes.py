import os
from omegaconf import OmegaConf
import torch
import torch.nn.functional as F
from .scripts.evaluation.funcs import load_model_checkpoint, get_latent_z
from .utils.utils import instantiate_from_config
from einops import repeat
import folder_paths
import comfy.model_management as mm
import comfy.utils
from contextlib import nullcontext
from .lvdm.models.samplers.ddim import DDIMSampler

def split_and_trim(input_string):
    # Split the string into an array using '|' as a separator
    array = input_string.split('|')
    
    # Trim white space from each element in the array
    trimmed_array = [element.strip() for element in array]
    
    return trimmed_array

def convert_dtype(dtype_str):
    if dtype_str == 'fp32':
        return torch.float32
    elif dtype_str == 'fp16':
        return torch.float16
    elif dtype_str == 'bf16':
        return torch.bfloat16
    else:
        raise NotImplementedError
    
script_directory = os.path.dirname(os.path.abspath(__file__))

class DynamiCrafterModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "ckpt_name": (folder_paths.get_filename_list("checkpoints"), ),
            "dtype": (
                    [
                        'fp32',
                        'fp16',
                        'bf16',
                        'auto'
                    ], {
                        "default": 'auto'
                    }),
            },
        }

    RETURN_TYPES = ("DCMODEL",)
    RETURN_NAMES = ("DynCraft_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "DynamiCrafterWrapper"

    def loadmodel(self, dtype, ckpt_name):
        mm.soft_empty_cache()
        custom_config = {
            'dtype': dtype,
            'ckpt_name': ckpt_name,
        }
        if not hasattr(self, 'model') or self.model == None or custom_config != self.current_config:
            self.current_config = custom_config
            model_path = folder_paths.get_full_path("checkpoints", ckpt_name)
            ckpt_base_name = os.path.basename(ckpt_name)
            base_name, _ = os.path.splitext(ckpt_base_name)
            if 'interp' in base_name and '512' in base_name:
                config_file=os.path.join(script_directory, "configs", "dynamicrafter_512_interp_v1.yaml")
            elif '1024' in base_name:
                config_file=os.path.join(script_directory, "configs", "dynamicrafter_1024_v1.yaml")
            elif '512' in base_name:
                config_file=os.path.join(script_directory, "configs", "dynamicrafter_512_v1.yaml")
            elif '256' in base_name:
                config_file=os.path.join(script_directory, "configs", "dynamicrafter_256_v1.yaml")
            else:
                print(f"No matching config for model: {ckpt_name}")
            config = OmegaConf.load(config_file)
            model_config = config.pop("model", OmegaConf.create())
            model_config['params']['unet_config']['params']['use_checkpoint']=False   
            self.model = instantiate_from_config(model_config)
            self.model = load_model_checkpoint(self.model, model_path)
            self.model.eval()
            if dtype == "auto":
                try:
                    if mm.should_use_fp16():
                        self.model.to(convert_dtype('fp16'))
                    elif mm.should_use_bf16():
                        self.model.to(convert_dtype('bf16'))
                    else:
                        self.model.to(convert_dtype('fp32'))
                except:
                    raise AttributeError("ComfyUI version too old, can't autodetect properly. Set your dtype manually.")
            else:
                self.model.to(convert_dtype(dtype))
            print(f"Model using dtype: {self.model.dtype}")
        return (self.model,)
    
class DynamiCrafterI2V:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "model": ("DCMODEL",),
            "image": ("IMAGE",),
            "steps": ("INT", {"default": 50, "min": 1, "max": 200, "step": 1}),
            "cfg": ("FLOAT", {"default": 7.0, "min": 0.0, "max": 20.0, "step": 0.01}),
            "eta": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            "frames": ("INT", {"default": 16, "min": 1, "max": 100, "step": 1}),
            "prompt": ("STRING", {"multiline": True, "default": "",}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "fs": ("INT", {"default": 10, "min": 2, "max": 100, "step": 1}),
            "keep_model_loaded": ("BOOLEAN", {"default": True}),
            "vae_dtype": (
                    [
                        'fp32',
                        'fp16',
                        'bf16',
                        'auto'
                    ], {
                        "default": 'auto'
                    }),
            
            },
            "optional": {
                "image2": ("IMAGE",),
                "mask": ("MASK",),
                "frame_window_size": ("INT", {"default": 16, "min": 1, "max": 200, "step": 1}),
                "frame_window_stride": ("INT", {"default": 4, "min": 1, "max": 200, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE", "IMAGE",)
    RETURN_NAMES = ("images", "last_image",)
    FUNCTION = "process"
    CATEGORY = "DynamiCrafterWrapper"

    def process(self, model, image, prompt, cfg, steps, eta, seed, fs, keep_model_loaded, frames, vae_dtype, frame_window_size=16, frame_window_stride=4, mask=None, image2=None):
        device = mm.get_torch_device()
        mm.unload_all_models()
        mm.soft_empty_cache()

        torch.manual_seed(seed)
        dtype = model.dtype
        if vae_dtype == "auto":
            try:
                if mm.should_use_bf16():
                    model.first_stage_model.to(convert_dtype('bf16'))
                else:
                    model.first_stage_model.to(convert_dtype('fp32'))
            except:
                raise AttributeError("ComfyUI version too old, can't autodetect properly. Set your dtype manually.")
        else:
            model.first_stage_model.to(convert_dtype(vae_dtype))
        print(f"VAE using dtype: {model.first_stage_model.dtype}")

        self.model = model
        self.model.to(device)
        autocast_condition = (dtype != torch.float32) and not comfy.model_management.is_device_mps(device)
        with torch.autocast(comfy.model_management.get_autocast_device(device), dtype=dtype) if autocast_condition else nullcontext():
            image = image * 2 - 1
            image = image.permute(0, 3, 1, 2).to(dtype).to(device)

            B, C, H, W = image.shape
            orig_H, orig_W = H, W
            if W % 64 != 0:
                W = W - (W % 64)
            if H % 64 != 0:
                H = H - (H % 64)
            if orig_H % 64 != 0 or orig_W % 64 != 0:
                image = F.interpolate(image, size=(H, W), mode="bicubic")
           
            B, C, H, W = image.shape
            noise_shape = [B, self.model.model.diffusion_model.out_channels, frames, H // 8, W // 8]

            self.model.first_stage_model.to(device)

            z = get_latent_z(self.model, image.unsqueeze(2)) #bc,1,hw

            if image2 is not None:
                image2 = image2 * 2 - 1
                image2 = image2.permute(0, 3, 1, 2).to(dtype).to(device)
                if image2.shape != image.shape:
                    image2 = F.interpolate(image, size=(H, W), mode="bicubic")
                z2 = get_latent_z(self.model, image2.unsqueeze(2)) #bc,1,hw
                img_tensor_repeat = repeat(z, 'b c t h w -> b c (repeat t) h w', repeat=frames)
                img_tensor_repeat = torch.zeros_like(img_tensor_repeat)
                img_tensor_repeat[:,:,:1,:,:] = z
                img_tensor_repeat[:,:,-1:,:,:] = z2
            else:
                img_tensor_repeat = repeat(z, 'b c t h w -> b c (repeat t) h w', repeat=frames)

            self.model.first_stage_model.to('cpu')

            self.model.cond_stage_model.to(device)
            self.model.embedder.to(device)
            self.model.image_proj_model.to(device)

            text_emb = self.model.get_learned_conditioning([prompt])
            cond_images = self.model.embedder(image)
            img_emb = self.model.image_proj_model(cond_images)
            imtext_cond = torch.cat([text_emb, img_emb], dim=1)
            del cond_images, img_emb, text_emb

            fs = torch.tensor([fs], dtype=torch.long, device=self.model.device)
            cond = {"c_crossattn": [imtext_cond], "c_concat": [img_tensor_repeat]}

            if noise_shape[-1] == 32:
                timestep_spacing = "uniform"
                guidance_rescale = 0.0
            else:
                timestep_spacing = "uniform_trailing"
                guidance_rescale = 0.7

            ## construct unconditional guidance
            if cfg != 1.0: 
                uc_emb = self.model.get_learned_conditioning([""])
                ## process image embedding token
                if hasattr(self.model, 'embedder'):
                    uc_img = torch.zeros(noise_shape[0],3,224,224).to(self.model.device)
                    ## img: b c h w >> b l c
                    uc_img = self.model.embedder(uc_img)
                    uc_img = self.model.image_proj_model(uc_img)
                    uc_emb = torch.cat([uc_emb, uc_img], dim=1)
                if isinstance(cond, dict):
                    uc = {key:cond[key] for key in cond.keys()}
                    uc.update({'c_crossattn': [uc_emb]})
                else:
                    uc = uc_emb
            else:
                uc = None

            self.model.cond_stage_model.to('cpu')
            self.model.embedder.to('cpu')
            self.model.image_proj_model.to('cpu')

            if mask is not None:
                mask = mask.to(dtype).to(device)
                mask = F.interpolate(mask.unsqueeze(0), size=(H // 8, W // 8), mode="nearest")
                mask = mask.squeeze(0)
                mask = (1 - mask)

            #inference
            ddim_sampler = DDIMSampler(self.model)
            samples, _ = ddim_sampler.sample(S=steps,
                                            conditioning=cond,
                                            batch_size=noise_shape[0],
                                            shape=noise_shape[1:],
                                            verbose=True,
                                            unconditional_guidance_scale=cfg,
                                            unconditional_conditioning=uc,
                                            eta=eta,
                                            temporal_length=noise_shape[2],
                                            conditional_guidance_scale_temporal=None,
                                            x_T=None,
                                            fs=fs,
                                            timestep_spacing=timestep_spacing,
                                            guidance_rescale=guidance_rescale,
                                            clean_cond=True,
                                            mask=mask,
                                            x0=z if mask is not None else None,
                                            frame_window_size = frame_window_size,
                                            frame_window_stride = frame_window_stride
                                            )
            
            assert not torch.isnan(samples).any().item(), "Resulting tensor containts NaNs. I'm unsure why this happens, changing step count and/or image dimensions might help."

            ## reconstruct from latent to pixel space
            self.model.first_stage_model.to(device)
            decoded_images = self.model.decode_first_stage(samples) #b c t h w
            self.model.first_stage_model.to('cpu')
        
            video = decoded_images.detach().cpu()
            video = torch.clamp(video.float(), -1., 1.)
            video = (video + 1.0) / 2.0
            video = video.squeeze(0).permute(1, 2, 3, 0)
            del decoded_images, samples

            if not keep_model_loaded:
                self.model.to('cpu')
                mm.soft_empty_cache()
            # Ensure the final dimensions are divisible by 2
            final_H = (orig_H // 2) * 2
            final_W = (orig_W // 2) * 2

            if video.shape[1] != final_H or video.shape[2] != final_W:
                video = F.interpolate(video.permute(0, 3, 1, 2), size=(final_H, final_W), mode="bicubic").permute(0, 2, 3, 1)
            last_image = video[-1].unsqueeze(0)
            return (video, last_image)
        
class DynamiCrafterBatchInterpolation:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "model": ("DCMODEL",),
            "images": ("IMAGE",),
            "steps": ("INT", {"default": 50, "min": 1, "max": 200, "step": 1}),
            "cfg": ("FLOAT", {"default": 7.0, "min": 0.0, "max": 20.0, "step": 0.01}),
            "eta": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 20.0, "step": 0.01}),
            "frames": ("INT", {"default": 16, "min": 1, "max": 100, "step": 1}),
            "prompt": ("STRING", {"multiline": True, "default": "",}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "fs": ("INT", {"default": 10, "min": 2, "max": 100, "step": 1}),
            "keep_model_loaded": ("BOOLEAN", {"default": True}),
            "vae_dtype": (
                    [
                        'fp32',
                        'fp16',
                        'bf16',
                        'auto'
                    ], {
                        "default": 'auto'
                    }),
            "cut_near_keyframes": ("INT", {"default": 0, "min": 0, "max": 5, "step": 1}),
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE",)
    RETURN_NAMES = ("images", "last_image",)
    FUNCTION = "process"
    CATEGORY = "DynamiCrafterWrapper"

    def process(self, model, images, prompt, cfg, steps, eta, seed, fs, keep_model_loaded, frames, vae_dtype, cut_near_keyframes):
        assert images.shape[0] > 1, "DynamiCrafterBatchInterpolation needs at least 2 images"
        device = mm.get_torch_device()
        mm.unload_all_models()
        mm.soft_empty_cache()

        torch.manual_seed(seed)
        dtype = model.dtype

        if vae_dtype == "auto":
            try:
                if mm.should_use_bf16():
                    model.first_stage_model.to(convert_dtype('bf16'))
                else:
                    model.first_stage_model.to(convert_dtype('fp32'))
            except:
                raise AttributeError("ComfyUI version too old, can't autodetect properly. Set your dtype manually.")
        else:
            model.first_stage_model.to(convert_dtype(vae_dtype))
        print(f"VAE using dtype: {model.first_stage_model.dtype}")

        self.model = model        
        self.model.to(device)
        images = images * 2 - 1
        images = images.permute(0, 3, 1, 2).to(dtype).to(device)
        B, C, H, W = images.shape
        orig_H, orig_W = H, W
        if W % 64 != 0:
            W = W - (W % 64)
        if H % 64 != 0:
            H = H - (H % 64)
        if orig_H % 64 != 0 or orig_W % 64 != 0:
            images = F.interpolate(images, size=(H, W), mode="bicubic")        
		
        split_prompt = split_and_trim(prompt)
        out = []
        autocast_condition = (dtype != torch.float32) and not comfy.model_management.is_device_mps(device)
        with torch.autocast(comfy.model_management.get_autocast_device(device), dtype=dtype) if autocast_condition else nullcontext():
            for i in range(len(images) - 1):

                image = images[i].unsqueeze(0)
                image2 = images[i+1].unsqueeze(0)
                B, C, H, W = image.shape
                noise_shape = [B, self.model.model.diffusion_model.out_channels, frames, H // 8, W // 8]

                self.model.first_stage_model.to(device)

                z = get_latent_z(self.model, image.unsqueeze(2)) #bc,1,hw
                z2 = get_latent_z(self.model, image2.unsqueeze(2)) #bc,1,hw
                img_tensor_repeat = repeat(z, 'b c t h w -> b c (repeat t) h w', repeat=frames)
                img_tensor_repeat = torch.zeros_like(img_tensor_repeat)
                img_tensor_repeat[:,:,:1,:,:] = z
                img_tensor_repeat[:,:,-1:,:,:] = z2
         
                self.model.first_stage_model.to('cpu')

                self.model.cond_stage_model.to(device)
                self.model.embedder.to(device)
                self.model.image_proj_model.to(device)
                
                try:
                    text_emb = self.model.get_learned_conditioning([split_prompt[i]])
                    print("Prompt: ", split_prompt[i])
                except:
                    text_emb = self.model.get_learned_conditioning([split_prompt[0]])
                    print("Prompt: ", split_prompt[0])

                cond_images = self.model.embedder(image)
                img_emb = self.model.image_proj_model(cond_images)
                imtext_cond = torch.cat([text_emb, img_emb], dim=1)

                fs = torch.tensor([fs], dtype=torch.long, device=self.model.device)
                cond = {"c_crossattn": [imtext_cond], "c_concat": [img_tensor_repeat]}

                if noise_shape[-1] == 32:
                    timestep_spacing = "uniform"
                    guidance_rescale = 0.0
                else:
                    timestep_spacing = "uniform_trailing"
                    guidance_rescale = 0.7

                ## construct unconditional guidance
                if cfg != 1.0: 
                    uc_emb = self.model.get_learned_conditioning([""])
                    ## process image embedding token
                    if hasattr(self.model, 'embedder'):
                        uc_img = torch.zeros(noise_shape[0],3,224,224).to(self.model.device)
                        ## img: b c h w >> b l c
                        uc_img = self.model.embedder(uc_img)
                        uc_img = self.model.image_proj_model(uc_img)
                        uc_emb = torch.cat([uc_emb, uc_img], dim=1)
                    if isinstance(cond, dict):
                        uc = {key:cond[key] for key in cond.keys()}
                        uc.update({'c_crossattn': [uc_emb]})
                    else:
                        uc = uc_emb
                else:
                    uc = None

                self.model.cond_stage_model.to('cpu')
                self.model.embedder.to('cpu')
                self.model.image_proj_model.to('cpu')

                #inference
                ddim_sampler = DDIMSampler(self.model)
                samples, _ = ddim_sampler.sample(S=steps,
                                                conditioning=cond,
                                                batch_size=noise_shape[0],
                                                shape=noise_shape[1:],
                                                verbose=True,
                                                unconditional_guidance_scale=cfg,
                                                unconditional_conditioning=uc,
                                                eta=eta,
                                                temporal_length=noise_shape[2],
                                                conditional_guidance_scale_temporal=None,
                                                x_T=None,
                                                fs=fs,
                                                timestep_spacing=timestep_spacing,
                                                guidance_rescale=guidance_rescale,
                                                clean_cond=True
                                                )
                
                assert not torch.isnan(samples).any().item(), "Resulting tensor containts NaNs. I'm unsure why this happens, changing step count and/or image dimensions might help."
                ## reconstruct from latent to pixel space
                self.model.first_stage_model.to(device)
                decoded_images = self.model.decode_first_stage(samples) #b c t h w
                self.model.first_stage_model.to('cpu')
            
                video = decoded_images.detach().cpu()
                video = torch.clamp(video.float(), -1., 1.)
                video = (video + 1.0) / 2.0
                video = video.squeeze(0).permute(1, 2, 3, 0)
                print(f"Sampled {i+1} / {len(images) - 1}")
                out.append(video)

            if not keep_model_loaded:
                self.model.to('cpu')
                mm.soft_empty_cache()
            out_video = torch.cat(out, dim=0)

            # Ensure the final dimensions are divisible by 2
            final_H = (orig_H // 2) * 2
            final_W = (orig_W // 2) * 2

            if out_video.shape[1] != final_H or out_video.shape[2] != final_W:
                out_video = F.interpolate(out_video.permute(0, 3, 1, 2), size=(final_H, final_W), mode="bicubic").permute(0, 2, 3, 1)

            # should we trim middle keyframes?
            if cut_near_keyframes > 0:
                already_deleted = 0
                for i in range(len(images) - 2):
                    old_size = out_video.shape[0]
                    keyframe_index = (i + 1) * frames - already_deleted
                    start_index = keyframe_index - (cut_near_keyframes // 2)
                    end_index = start_index + cut_near_keyframes
                    out_video = torch.cat([out_video[:start_index], out_video[end_index:]], dim=0)
                    already_deleted += old_size - out_video.shape[0]

            last_image = out_video[-1].unsqueeze(0)
            return (out_video, last_image)

NODE_CLASS_MAPPINGS = {
    "DynamiCrafterI2V": DynamiCrafterI2V,
    "DynamiCrafterModelLoader": DynamiCrafterModelLoader,
    "DynamiCrafterBatchInterpolation": DynamiCrafterBatchInterpolation

}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DynamiCrafterI2V": "DynamiCrafterI2V",
    "DynamiCrafterModelLoader": "DynamiCrafterModelLoader",
    "DynamiCrafterBatchInterpolation": "DynamiCrafterBatchInterpolation"
}
