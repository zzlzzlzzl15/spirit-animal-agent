"""生成默认灵狐精灵图 — 8col x 9row = 1536x1872px。"""

from PIL import Image, ImageDraw
import os

FRAME_W, FRAME_H = 192, 208
COLS, ROWS = 8, 9
sheet = Image.new('RGBA', (FRAME_W * COLS, FRAME_H * ROWS), (0, 0, 0, 0))
draw = ImageDraw.Draw(sheet)

ORANGE = (255, 140, 50, 255)
LIGHT_ORANGE = (255, 180, 120, 255)
WHITE = (255, 255, 255, 255)
BLACK = (40, 40, 40, 255)
PINK = (255, 180, 180, 255)

row_colors = [
    ORANGE, ORANGE, ORANGE, LIGHT_ORANGE,
    (255, 200, 50, 255), (200, 100, 100, 255),
    (150, 150, 200, 255), ORANGE, (180, 140, 255, 255),
]

def draw_fox(draw, cx, cy, scale=1.0, color=ORANGE,
             eye_style='normal', mouth_style='smile', ear_offset=0):
    s = scale
    # 身体
    draw.ellipse([cx-28*s, cy-10*s, cx+28*s, cy+40*s], fill=color)
    # 头
    draw.ellipse([cx-32*s, cy-50*s, cx+32*s, cy+10*s], fill=color)
    # 耳朵
    draw.polygon([
        (cx-28*s, cy-40*s+ear_offset),
        (cx-38*s, cy-70*s+ear_offset),
        (cx-10*s, cy-45*s+ear_offset)
    ], fill=color)
    draw.polygon([
        (cx+28*s, cy-40*s+ear_offset),
        (cx+38*s, cy-70*s+ear_offset),
        (cx+10*s, cy-45*s+ear_offset)
    ], fill=color)
    # 内耳
    draw.polygon([
        (cx-24*s, cy-42*s+ear_offset),
        (cx-33*s, cy-62*s+ear_offset),
        (cx-14*s, cy-44*s+ear_offset)
    ], fill=LIGHT_ORANGE)
    draw.polygon([
        (cx+24*s, cy-42*s+ear_offset),
        (cx+33*s, cy-62*s+ear_offset),
        (cx+14*s, cy-44*s+ear_offset)
    ], fill=LIGHT_ORANGE)
    # 白色面部
    draw.ellipse([cx-20*s, cy-25*s, cx+20*s, cy+5*s], fill=WHITE)
    # 眼睛
    if eye_style == 'normal':
        draw.ellipse([cx-14*s, cy-20*s, cx-6*s, cy-10*s], fill=BLACK)
        draw.ellipse([cx+6*s, cy-20*s, cx+14*s, cy-10*s], fill=BLACK)
        draw.ellipse([cx-12*s, cy-18*s, cx-9*s, cy-15*s], fill=WHITE)
        draw.ellipse([cx+8*s, cy-18*s, cx+11*s, cy-15*s], fill=WHITE)
    elif eye_style == 'happy':
        draw.arc([cx-14*s, cy-22*s, cx-6*s, cy-12*s], 200, 340,
                 fill=BLACK, width=max(1, int(2*s)))
        draw.arc([cx+6*s, cy-22*s, cx+14*s, cy-12*s], 200, 340,
                 fill=BLACK, width=max(1, int(2*s)))
    elif eye_style == 'sad':
        draw.ellipse([cx-14*s, cy-18*s, cx-6*s, cy-8*s], fill=BLACK)
        draw.ellipse([cx+6*s, cy-18*s, cx+14*s, cy-8*s], fill=BLACK)
    elif eye_style == 'closed':
        draw.line([(cx-14*s, cy-15*s), (cx-6*s, cy-15*s)],
                  fill=BLACK, width=max(1, int(2*s)))
        draw.line([(cx+6*s, cy-15*s), (cx+14*s, cy-15*s)],
                  fill=BLACK, width=max(1, int(2*s)))
    # 鼻子
    draw.ellipse([cx-4*s, cy-5*s, cx+4*s, cy+2*s], fill=BLACK)
    # 嘴巴
    if mouth_style == 'smile':
        draw.arc([cx-8*s, cy-2*s, cx+8*s, cy+8*s], 10, 170,
                 fill=BLACK, width=max(1, int(1.5*s)))
    elif mouth_style == 'open':
        draw.ellipse([cx-6*s, cy, cx+6*s, cy+8*s], fill=PINK)
    elif mouth_style == 'sad':
        draw.arc([cx-8*s, cy+2*s, cx+8*s, cy+12*s], 190, 350,
                 fill=BLACK, width=max(1, int(1.5*s)))
    # 尾巴
    draw.arc([cx+15*s, cy+10*s, cx+55*s, cy+50*s], 200, 350,
             fill=color, width=max(2, int(8*s)))

# 8 帧动画参数
y_breath = [0, -2, -3, -2, 0, 2, 3, 2]
x_run_r = [-15, -8, 0, 8, 15, 8, 0, -8]
x_run_l = [15, 8, 0, -8, -15, -8, 0, 8]
ear_wave = [0, -3, -6, -8, -6, -3, 0, 3]
y_jump = [0, -10, -20, -28, -25, -15, -5, 0]
y_work = [0, -1, -2, -3, -2, -1, 0, 1]

for row in range(ROWS):
    for col in range(COLS):
        cx = col * FRAME_W + FRAME_W // 2
        cy = row * FRAME_H + FRAME_H // 2 + 10
        color = row_colors[row]

        if row == 0:  # idle
            draw_fox(draw, cx, cy + y_breath[col], color=color)
        elif row == 1:  # run-right
            draw_fox(draw, cx + x_run_r[col], cy, color=color, mouth_style='open')
        elif row == 2:  # run-left
            draw_fox(draw, cx + x_run_l[col], cy, color=color, mouth_style='open')
        elif row == 3:  # wave
            draw_fox(draw, cx, cy, color=color, eye_style='happy',
                     ear_offset=ear_wave[col])
        elif row == 4:  # jump
            draw_fox(draw, cx, cy + y_jump[col], color=color,
                     eye_style='happy', mouth_style='open')
        elif row == 5:  # failed
            draw_fox(draw, cx, cy, color=color, eye_style='sad', mouth_style='sad')
        elif row == 6:  # waiting
            draw_fox(draw, cx, cy, color=color, eye_style='closed')
        elif row == 7:  # running/working
            draw_fox(draw, cx, cy + y_work[col], color=color, mouth_style='open')
        elif row == 8:  # review/thinking
            draw_fox(draw, cx, cy, color=color, eye_style='closed',
                     mouth_style='smile')

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
out_path = os.path.join(project_root,
                        'desktop-app', 'assets', 'pets', 'spirit-fox', 'spritesheet.png')
os.makedirs(os.path.dirname(out_path), exist_ok=True)
sheet.save(out_path)
print(f'Sprite sheet saved: {out_path} ({sheet.width}x{sheet.height})')
