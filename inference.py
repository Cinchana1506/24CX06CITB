import os 
import cv2 
import glob
from pathlib import Path
import utils
import argparse
import numpy as np
import tempfile
import torch

from utils import convert_state_dict
from models import restormer_arch
from data.preprocess.crop_merge_image import stride_integral

os.sys.path.append('./data/MBD/')
from data.MBD.infer import net1_net2_infer_single_im


def dewarp_prompt(img):
    mask = net1_net2_infer_single_im(img,'data/MBD/checkpoint/mbd.pkl')
    base_coord = utils.getBasecoord(256,256)/256
    img[mask==0]=0
    mask = cv2.resize(mask,(256,256))/255
    return img,np.concatenate((base_coord,np.expand_dims(mask,-1)),-1)

def deshadow_prompt(img):
    h,w = img.shape[:2]
    # img = cv2.resize(img,(128,128))
    img = cv2.resize(img,(1024,1024))
    rgb_planes = cv2.split(img)
    result_planes = []
    result_norm_planes = []
    bg_imgs = []
    for plane in rgb_planes:
        dilated_img = cv2.dilate(plane, np.ones((7,7), np.uint8))
        bg_img = cv2.medianBlur(dilated_img, 21)
        bg_imgs.append(bg_img)
        diff_img = 255 - cv2.absdiff(plane, bg_img)
        norm_img = cv2.normalize(diff_img,None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8UC1)
        result_planes.append(diff_img)
        result_norm_planes.append(norm_img)
    bg_imgs = cv2.merge(bg_imgs)
    bg_imgs = cv2.resize(bg_imgs,(w,h))
    # result = cv2.merge(result_planes)
    result_norm = cv2.merge(result_norm_planes)
    result_norm[result_norm==0]=1
    shadow_map = np.clip(img.astype(float)/result_norm.astype(float)*255,0,255).astype(np.uint8)
    shadow_map = cv2.resize(shadow_map,(w,h))
    shadow_map = cv2.cvtColor(shadow_map,cv2.COLOR_BGR2GRAY)
    shadow_map = cv2.cvtColor(shadow_map,cv2.COLOR_GRAY2BGR)
    # return shadow_map
    return bg_imgs

def deblur_prompt(img):
    x = cv2.Sobel(img,cv2.CV_16S,1,0)  
    y = cv2.Sobel(img,cv2.CV_16S,0,1)  
    absX = cv2.convertScaleAbs(x)     
    absY = cv2.convertScaleAbs(y)  
    high_frequency = cv2.addWeighted(absX,0.5,absY,0.5,0)
    high_frequency = cv2.cvtColor(high_frequency,cv2.COLOR_BGR2GRAY)
    high_frequency = cv2.cvtColor(high_frequency,cv2.COLOR_GRAY2BGR)
    return high_frequency

def appearance_prompt(img):
    h,w = img.shape[:2]
    # img = cv2.resize(img,(128,128))
    img = cv2.resize(img,(1024,1024))
    rgb_planes = cv2.split(img)
    result_planes = []
    result_norm_planes = []
    for plane in rgb_planes:
        dilated_img = cv2.dilate(plane, np.ones((7,7), np.uint8))
        bg_img = cv2.medianBlur(dilated_img, 21)
        diff_img = 255 - cv2.absdiff(plane, bg_img)
        norm_img = cv2.normalize(diff_img,None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8UC1)
        result_planes.append(diff_img)
        result_norm_planes.append(norm_img)
    result_norm = cv2.merge(result_norm_planes)
    result_norm = cv2.resize(result_norm,(w,h))
    return result_norm

def binarization_promptv2(img):
    result,thresh = utils.SauvolaModBinarization(img)
    thresh = thresh.astype(np.uint8)
    result[result>155]=255
    result[result<=155]=0

    x = cv2.Sobel(img,cv2.CV_16S,1,0)  
    y = cv2.Sobel(img,cv2.CV_16S,0,1)  
    absX = cv2.convertScaleAbs(x)     
    absY = cv2.convertScaleAbs(y)  
    high_frequency = cv2.addWeighted(absX,0.5,absY,0.5,0)
    high_frequency = cv2.cvtColor(high_frequency,cv2.COLOR_BGR2GRAY)
    return np.concatenate((np.expand_dims(thresh,-1),np.expand_dims(high_frequency,-1),np.expand_dims(result,-1)),-1)

def dewarping(model,im_path):
    INPUT_SIZE=256
    im_org = cv2.imread(im_path)
    im_masked, prompt_org = dewarp_prompt(im_org.copy())

    h,w = im_masked.shape[:2]
    im_masked = im_masked.copy()
    im_masked = cv2.resize(im_masked,(INPUT_SIZE,INPUT_SIZE))
    im_masked = im_masked / 255.0
    im_masked = torch.from_numpy(im_masked.transpose(2,0,1)).unsqueeze(0)
    im_masked = im_masked.float().to(DEVICE)

    prompt = torch.from_numpy(prompt_org.transpose(2,0,1)).unsqueeze(0)
    prompt = prompt.float().to(DEVICE)

    in_im = torch.cat((im_masked,prompt),dim=1)

    # inference
    base_coord = utils.getBasecoord(INPUT_SIZE,INPUT_SIZE)/INPUT_SIZE
    model = model.float()
    with torch.no_grad():
        pred = model(in_im)
        pred = pred[0][:2].permute(1,2,0).cpu().numpy()
        pred = pred+base_coord
    ## smooth
    for i in range(15):
        pred = cv2.blur(pred,(3,3),borderType=cv2.BORDER_REPLICATE) 
    pred = cv2.resize(pred,(w,h))*(w,h)
    pred = pred.astype(np.float32)
    out_im = cv2.remap(im_org,pred[:,:,0],pred[:,:,1],cv2.INTER_LINEAR)

    prompt_org = (prompt_org*255).astype(np.uint8)
    prompt_org = cv2.resize(prompt_org,im_org.shape[:2][::-1])

    return prompt_org[:,:,0],prompt_org[:,:,1],prompt_org[:,:,2],out_im

def appearance(model,im_path):
    MAX_SIZE=1600
    # obtain im and prompt
    im_org = cv2.imread(im_path)
    h,w = im_org.shape[:2]
    prompt = appearance_prompt(im_org)
    in_im = np.concatenate((im_org,prompt),-1)

    # constrain the max resolution 
    if max(w,h) < MAX_SIZE:
        in_im,padding_h,padding_w = stride_integral(in_im,8)
    else:
        in_im = cv2.resize(in_im,(MAX_SIZE,MAX_SIZE))
    
    # normalize
    in_im = in_im / 255.0
    in_im = torch.from_numpy(in_im.transpose(2,0,1)).unsqueeze(0)

    # inference
    in_im = in_im.half().to(DEVICE)
    model = model.half()
    with torch.no_grad():
        pred = model(in_im)
        pred = torch.clamp(pred,0,1)
        pred = pred[0].permute(1,2,0).cpu().numpy()
        pred = (pred*255).astype(np.uint8)

        if max(w,h) < MAX_SIZE:
            out_im = pred[padding_h:,padding_w:]
        else:
            pred[pred==0] = 1
            shadow_map = cv2.resize(im_org,(MAX_SIZE,MAX_SIZE)).astype(float)/pred.astype(float)
            shadow_map = cv2.resize(shadow_map,(w,h))
            shadow_map[shadow_map==0]=0.00001
            out_im = np.clip(im_org.astype(float)/shadow_map,0,255).astype(np.uint8)

    return prompt[:,:,0],prompt[:,:,1],prompt[:,:,2],out_im
        

def deshadowing(model,im_path):
    MAX_SIZE=1600
    # obtain im and prompt
    im_org = cv2.imread(im_path)
    h,w = im_org.shape[:2]
    prompt = deshadow_prompt(im_org)
    in_im = np.concatenate((im_org,prompt),-1)

    # constrain the max resolution 
    if max(w,h) < MAX_SIZE:
        in_im,padding_h,padding_w = stride_integral(in_im,8)
    else:
        in_im = cv2.resize(in_im,(MAX_SIZE,MAX_SIZE))
    
    # normalize
    in_im = in_im / 255.0
    in_im = torch.from_numpy(in_im.transpose(2,0,1)).unsqueeze(0)

    # inference
    in_im = in_im.half().to(DEVICE)
    model = model.half()
    with torch.no_grad():
        pred = model(in_im)
        pred = torch.clamp(pred,0,1)
        pred = pred[0].permute(1,2,0).cpu().numpy()
        pred = (pred*255).astype(np.uint8)

        if max(w,h) < MAX_SIZE:
            out_im = pred[padding_h:,padding_w:]
        else:
            pred[pred==0]=1
            shadow_map = cv2.resize(im_org,(MAX_SIZE,MAX_SIZE)).astype(float)/pred.astype(float)
            shadow_map = cv2.resize(shadow_map,(w,h))
            shadow_map[shadow_map==0]=0.00001
            out_im = np.clip(im_org.astype(float)/shadow_map,0,255).astype(np.uint8)

    return prompt[:,:,0],prompt[:,:,1],prompt[:,:,2],out_im


def deblurring(model,im_path):
    # setup image
    im_org = cv2.imread(im_path)
    in_im,padding_h,padding_w = stride_integral(im_org,8)
    prompt = deblur_prompt(in_im)
    in_im = np.concatenate((in_im,prompt),-1)
    in_im = in_im / 255.0
    in_im = torch.from_numpy(in_im.transpose(2,0,1)).unsqueeze(0)
    in_im = in_im.half().to(DEVICE)  
    # inference
    model.to(DEVICE)
    model.eval()
    model = model.half()
    with torch.no_grad():
        pred = model(in_im)
        pred = torch.clamp(pred,0,1)
        pred = pred[0].permute(1,2,0).cpu().numpy()
        pred = (pred*255).astype(np.uint8)
        out_im = pred[padding_h:,padding_w:]
    
    return prompt[:,:,0],prompt[:,:,1],prompt[:,:,2],out_im



def binarization(model,im_path):
    im_org = cv2.imread(im_path)
    im,padding_h,padding_w = stride_integral(im_org,8)
    prompt = binarization_promptv2(im)
    h,w = im.shape[:2]
    in_im = np.concatenate((im,prompt),-1)

    in_im = in_im / 255.0
    in_im = torch.from_numpy(in_im.transpose(2,0,1)).unsqueeze(0)
    in_im = in_im.to(DEVICE)
    model = model.half()
    in_im = in_im.half()
    with torch.no_grad():
        pred = model(in_im)
        pred = pred[:,:2,:,:]
        pred = torch.max(torch.softmax(pred,1),1)[1]
        pred = pred[0].cpu().numpy()
        pred = (pred*255).astype(np.uint8)
        pred = cv2.resize(pred,(w,h))
        out_im = pred[padding_h:,padding_w:]
    
    return prompt[:,:,0],prompt[:,:,1],prompt[:,:,2],out_im

def get_args():
    parser = argparse.ArgumentParser(description='Params')
    parser.add_argument('--model_path', nargs='?', type=str, default='./checkpoints/model.pkl',help='Path of the saved checkpoint')
    parser.add_argument('--im_path', nargs='?', type=str, default='./distorted/',
                        help='Path of input document image')
    parser.add_argument('--out_folder', nargs='?', type=str, default='./restorted/',
                        help='Folder of the output images')
    parser.add_argument('--task', nargs='?', type=str, default='dewarping', 
                        help='task that need to be executed')
    parser.add_argument('--save_dtsprompt', nargs='?', type=int, default=0, 
                        help='Width of the input image')
    args = parser.parse_args()
    possible_tasks = ['dewarping','deshadowing','appearance','deblurring','binarization','end2end']
    assert args.task in possible_tasks, 'Unsupported task, task must be one of '+', '.join(possible_tasks)
    return args

def model_init(args):
   # prepare model
    model = restormer_arch.Restormer( 
        inp_channels=6, 
        out_channels=3, 
        dim = 48,
        num_blocks = [2,3,3,4], 
        num_refinement_blocks = 4,
        heads = [1,2,4,8],
        ffn_expansion_factor = 2.66,
        bias = False,
        LayerNorm_type = 'WithBias',
        dual_pixel_task = True        
    )

    if DEVICE.type == 'cpu':
        state = convert_state_dict(torch.load(args.model_path, map_location='cpu')['model_state'])
    else:
        state = convert_state_dict(torch.load(args.model_path, map_location='cuda:0')['model_state'])    
    model.load_state_dict(state)

    model.eval()
    model = model.to(DEVICE)
    return model



def save_temp_image(image):
    temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    cv2.imwrite(temp_file.name, image)
    return temp_file.name  # Returns the path

def inference_one_im(model, im_path):
    print("Performing deshadowing...")
    _, _, _, intermediate = deshadowing(model, im_path)
    intermediate_path = save_temp_image(intermediate)

    print("Performing appearance enhancement...")
    _, _, _, intermediate = appearance(model, intermediate_path)
    intermediate_path = save_temp_image(intermediate)

    print("Performing deblurring...")
    _, _, _, restored = deblurring(model, intermediate_path)

    return restored




def save_results(
    restorted: np.ndarray,
    img_path: str,
    out_folder: str,
    task: str,
):
    """
    Save the final result to the output folder with a descriptive filename.
    """
    im_name = os.path.basename(img_path)
    im_format = '.' + im_name.split('.')[-1]
    save_path = os.path.join(out_folder, im_name.replace(im_format, '_' + task + im_format))
    cv2.imwrite(save_path, restorted)
    print(f"Output saved to: {save_path}")


if __name__ == '__main__':
    # Model initialization
    DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    args = get_args()
    model = model_init(args)

    img_source = args.im_path

    # Check if the input is a folder or a single image
    if Path(img_source).is_dir():
        img_paths = glob.glob(os.path.join(img_source, '*'))
        for img_path in img_paths:
            # Perform inference
            restorted = inference_one_im(model, img_path)

            # Save the final result
            save_results(
                restorted=restorted,
                img_path=img_path,
                out_folder=args.out_folder,
                task="deshadowing+appearance+deblurring",  # Updated task description
            )
    else:
        # Perform inference for a single image
        restorted = inference_one_im(model, img_source)

        # Save the final result
        save_results(
            restorted=restorted,
            img_path=img_source,
            out_folder=args.out_folder,
            task="deshadowing+appearance+deblurring",  # Updated task description
        )
