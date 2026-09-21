import io
import os
import threading
from PIL import Image


class Encoder:
    def __init__(self):
        import open_clip
        import torch
        self.torch=torch
        torch.set_num_threads(int(os.getenv("TORCH_THREADS","2")))
        self.device=os.getenv("CLIP_DEVICE","cpu")
        self.model,_,self.preprocess=open_clip.create_model_and_transforms("ViT-B-32",pretrained="openai",device=self.device,force_quick_gelu=True)
        self.model.eval()
        self.tokenize=open_clip.get_tokenizer("ViT-B-32")
        self.lock=threading.Lock()

    def normalized(self,features):
        features=features/features.norm(dim=-1,keepdim=True)
        return features[0].cpu().float().tolist()

    def image(self,data):
        with Image.open(io.BytesIO(data)) as image:
            tensor=self.preprocess(image.convert("RGB")).unsqueeze(0).to(self.device)
        with self.lock,self.torch.inference_mode():
            return self.normalized(self.model.encode_image(tensor))

    def text(self,text):
        tokens=self.tokenize([text]).to(self.device)
        with self.lock,self.torch.inference_mode():
            return self.normalized(self.model.encode_text(tokens))
