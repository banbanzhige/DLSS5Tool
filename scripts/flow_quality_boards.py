"""Comparison contact sheet and silent side-by-side clip for the flow probe."""
import argparse
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    p = args.directory
    meta = json.loads((p/'input.json').read_text(encoding='utf-8'))
    inputs = np.load(p/'inputs.npy', mmap_mode='r')
    names = ['zero','raft720_6','raft720_12','raft1024_6','sea720_4','sea1024_4']
    labels = ['无光流','RAFT 720 / 6','RAFT 720 / 12','RAFT 1024 / 6','SEA 720 / 4','SEA 1024 / 4']
    frame = 74
    index = meta['indices'].index(frame)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',22)
    canvas = Image.new('RGB',(1920,1150),'#141923')
    draw = ImageDraw.Draw(canvas)
    draw.text((16,10),'原片帧74 · 同参数 DLSS 输出与光流对照',font=font,fill='white')
    draw.text((16,44),'顶行缩略图；中间为眼部/刘海 1:1 裁剪；底行为统一8像素量程光流。色块不是分割精度。',font=font,fill='#c0cbd8')
    rois = [(540,650,852,862),(440,370,752,582)]
    for col,(name,label) in enumerate(zip(names,labels)):
        x = col*320+4
        draw.text((x,88),label,font=font,fill='white')
        im=Image.open(p/name/f'output-{frame}.png').convert('RGB')
        canvas.paste(im.resize((312,312),Image.Resampling.LANCZOS),(x,125))
        for row,roi in enumerate(rois):
            canvas.paste(im.crop(roi),(x,453+row*230))
        flow=Image.open(p/name/f'flow-{frame}.png').convert('RGB')
        canvas.paste(flow.resize((240,240)),(x,907))
    canvas.save(p/'comparison-frame074.png')
    # Side-by-side preview, preserving each original window's playback rate.
    video_names=['raft720_6','raft1024_6','sea720_4']
    arrays=[np.load(p/n/'outputs.npy',mmap_mode='r') for n in video_names]
    writer=cv2.VideoWriter(str(p/'comparison.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),meta['fps'],(1440,520))
    if not writer.isOpened():
        raise RuntimeError('Cannot create comparison video')
    for i in range(len(inputs)):
        board=np.full((520,1440,3),22,np.uint8)
        for col,(name,arr) in enumerate(zip(video_names,arrays)):
            board[40:,col*480:(col+1)*480]=cv2.resize(cv2.cvtColor(arr[i],cv2.COLOR_RGBA2BGR),(480,480))
            cv2.putText(board,f'{name} | source {meta["indices"][i]}',(col*480+10,27),cv2.FONT_HERSHEY_SIMPLEX,.6,(235,235,235),1,cv2.LINE_AA)
        writer.write(board)
    writer.release()
    cap=cv2.VideoCapture(str(p/'comparison.mp4'))
    count=0
    while cap.read()[0]:
        count+=1
    cap.release()
    assert count==len(inputs),count
    print(p/'comparison-frame074.png')
    print(p/'comparison.mp4')
    # Exact repeat checks, not visual claims from a lossy preview.
    if (p/'raft720_6_repeat/result.json').exists():
        repeat={}
        for file in ('flows.npy','outputs.npy'):
            a=np.load(p/'raft720_6'/file,mmap_mode='r')
            b=np.load(p/'raft720_6_repeat'/file,mmap_mode='r')
            repeat[file]={'equal':all(np.array_equal(a[i],b[i]) for i in range(len(a))),
                'max_abs':max(float(np.max(np.abs(a[i].astype(np.float32)-b[i].astype(np.float32)))) for i in range(len(a)))}
        (p/'repeat.json').write_text(json.dumps(repeat,indent=2),encoding='utf-8')
        print(repeat)


if __name__=='__main__':
    main()
