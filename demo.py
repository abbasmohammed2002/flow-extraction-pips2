import time
import numpy as np
import glob
import saverloader
from nets.pips2 import Pips
import utils.improc
from utils.basic import print_, print_stats
import torch
from tensorboardX import SummaryWriter
import torch.nn.functional as F
import torchvision.transforms as transforms
from fire import Fire
import sys
import cv2
import os
import random
from pathlib import Path
from skimage.morphology import skeletonize
from skimage import img_as_bool


def get_center_lines(anchor_frame):
    skeleton = skeletonize(anchor_frame)
    return skeleton

def skeleton_to_coordinates(skeleton):
    # Assuming skeleton is a numpy array of shape (512, 512, 1)
    # Flatten the skeleton to 2D
    skeleton_2d = skeleton[:, :, 0]

    # Find the coordinates of the white pixels
    y_coords, x_coords = np.nonzero(skeleton_2d)

    # Stack the coordinates into an (N, 2) array
    coordinates = np.stack((x_coords, y_coords), axis=-1)

    return coordinates

def read_mp4(fn):
    vidcap = cv2.VideoCapture(fn)
    frames = []
    while(vidcap.isOpened()):
        ret, frame = vidcap.read()
        if ret == False:
            break
        frames.append(frame)
    vidcap.release()
    return frames

def png2mp4(input_dir, output_dir, save_file_basename='dev', fps=15):
    os.makedirs(output_dir, exist_ok=True)

    # get all *.png files in the folder
    list_png_lists = glob.glob(os.path.join(input_dir, '*.png'))

    # sort the .png according to the idx of each frame
    def _sortFunc(e):
        file_name = os.path.basename(e)[:-4]  # Remove the '.png' extension
        print(f"file_name: {file_name}")
        parts = file_name.split('-')
        print(f"parts: {parts}")
        idx = int(parts[-1])  # Convert the last part to integer
        print(f"idx: {idx}")
        return idx

    list_png_lists.sort(key=_sortFunc)

    output_path = os.path.join(output_dir, save_file_basename + '.mp4')

    # create a video writer
    height, width = cv2.imread(list_png_lists[0]).shape[:-1]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # Specify video codec
    video_writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    for frame_path in list_png_lists:
        frame = cv2.imread(frame_path)
        frame_uint8 = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        video_writer.write(frame_uint8)

    video_writer.release()
    print(f"Saved to {output_path}")
    return output_path  # Return the path to the generated MP4 file


def read_frames(directory):
    frames = []

    # List all files in the directory and print them for debugging
    frame_files = os.listdir(directory)
    print("All files in the directory:", frame_files)

    for idx, img_name in enumerate(sorted(frame_files)):
        # Print the current file name being processed
        print("Processing file:", img_name)

        # Check if the current file is a .png file
        if img_name.endswith('.jpg'):
            img_path = os.path.join(directory, img_name)
            frame = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)  # Read as grayscale
            frame = frame[:, :, np.newaxis]  # Add a channel dimension

            # Check if the image was successfully read
            if frame is not None:
                print(frame.shape)
                frames.append(frame)
                print(f"Successfully read {img_name}")

            else:
                print(f"Failed to read image: {img_name}")
        else:
            print(f"Skipping non-png file: {img_name}")

    return frames[::-1]

def run_model(model, rgbs, S_max=128, N=64, iters=16, sw=None):
    rgbs = rgbs.cuda().float() # B, S, C, H, W

    B, S, C, H, W = rgbs.shape
    assert(B==1)

    # pick N points to track; we'll use a uniform grid
    N_ = np.sqrt(N).round().astype(np.int32)
    grid_y, grid_x = utils.basic.meshgrid2d(B, N_, N_, stack=False, norm=False, device='cuda')
    grid_y = 8 + grid_y.reshape(B, -1)/float(N_-1) * (H-16)
    grid_x = 8 + grid_x.reshape(B, -1)/float(N_-1) * (W-16)
    xy0 = torch.stack([grid_x, grid_y], dim=-1) # B, N_*N_, 2
    _, S, C, H, W = rgbs.shape

    # zero-vel init
    trajs_e = xy0.unsqueeze(1).repeat(1,S,1,1)

    iter_start_time = time.time()
    
    preds, preds_anim, _, _ = model(trajs_e, rgbs, iters=iters, feat_init=None, beautify=True)
    trajs_e = preds[-1]

    iter_time = time.time()-iter_start_time
    print('inference time: %.2f seconds (%.1f fps)' % (iter_time, S/iter_time))

    if sw is not None and sw.save_this:
        rgbs_prep = utils.improc.preprocess_color(rgbs)
        sw.summ_traj2ds_on_rgbs('outputs/trajs_on_rgbs', trajs_e[0:1], utils.improc.preprocess_color(rgbs[0:1]), cmap='hot', linewidth=1, show_dots=False)
    return trajs_e


def main(
        filename='./stock_videos/camel.mp4',
        S=48, # seqlen
        N=64, # number of points per clip
        stride=8, # spatial stride of the model
        timestride=1, # temporal stride of the model
        iters=16, # inference steps of the model
        image_size=(512,896), # input resolution
        max_iters=4, # number of clips to run
        shuffle=False, # dataset shuffling
        log_freq=1, # how often to make image summaries
        log_dir='./logs_demo',
        init_dir='./reference_model',
        device_ids=[0],
):

    # the idea in this file is to run the model on a demo video,
    # and return some visualizations
    
    exp_name = 'de00' # copy from dev repo

    print('filename', filename)
    name = Path(filename).stem
    print('name', name)

    inputdir = os.path.join(os.getcwd(), 'angiograms/angiogram_seg1')
    outputdir = os.path.join(os.getcwd(), 'angiograms/mp4video')
    mp4_file_path = png2mp4(inputdir, outputdir)
    print('mp4_file_path', mp4_file_path)
    rgbs = read_mp4(mp4_file_path)
    print('rgbs', len(rgbs))

    # anchor_frame = rgbs[0]
    # skeleton = get_center_lines(anchor_frame)
    # skeleton = skeleton_to_coordinates(skeleton)

    rgbs = np.stack(rgbs, axis=0) # S,H,W,3
    print(f"rgbs.shape: {rgbs.shape}")
    rgbs = rgbs[:,:,:,::-1].copy() # BGR->RGB
    print(f"rgbs.shape: {rgbs.shape}")
    rgbs = rgbs[::timestride]
    S_here,H,W,C = rgbs.shape
    print(f"rgbs.shape: {rgbs.shape}")

    # autogen a name
    model_name = "%s_%d_%d_%s" % (name, S, N, exp_name)
    import datetime
    model_date = datetime.datetime.now().strftime('%H:%M:%S')
    model_name = model_name + '_' + model_date
    print('model_name', model_name)
    
    log_dir = 'logs_demo'
    writer_t = SummaryWriter(log_dir + '/' + model_name + '/t', max_queue=10, flush_secs=60)

    global_step = 0

    model = Pips(stride=8).cuda()
    parameters = list(model.parameters())
    if init_dir:
        _ = saverloader.load(init_dir, model)
    global_step = 0
    model.eval()

    idx = list(range(0, max(S_here-S,1), S))
    if max_iters:
        idx = idx[:max_iters]
    
    for si in idx:
        global_step += 1
        
        iter_start_time = time.time()

        sw_t = utils.improc.Summ_writer(
            writer=writer_t,
            global_step=global_step,
            log_freq=log_freq,
            fps=16,
            scalar_freq=int(log_freq/2),
            just_gif=True)

        rgb_seq = rgbs[si:si+S]
        rgb_seq = torch.from_numpy(rgb_seq).permute(0,3,1,2).to(torch.float32) # S,3,H,W
        rgb_seq = F.interpolate(rgb_seq, image_size, mode='bilinear').unsqueeze(0) # 1,S,3,H,W
        
        with torch.no_grad():
            trajs_e = run_model(model, rgb_seq, S_max=S, N=N, iters=iters, sw=sw_t)

        iter_time = time.time()-iter_start_time
        
        print('%s; step %06d/%d; itime %.2f' % (
            model_name, global_step, max_iters, iter_time))
        
            
    writer_t.close()

if __name__ == '__main__':
    Fire(main)
