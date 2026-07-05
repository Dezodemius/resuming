#!/usr/bin/env python3
"""Генерация favicon-сета из зелёного logo-mark."""
import os
from PIL import Image, ImageDraw

# Цвета эталона
COLOR_START = (0x34, 0xd3, 0x99)  # #34d399
COLOR_END = (0x10, 0xb9, 0x81)    # #10b981
COLOR_DARK = (0x04, 0x13, 0x0d)   # #04130d - штрих документа

# Размеры
MASTER_SIZE = 1024
GRAD_SIZE = 256

def create_gradient(size):
    """Создать диагональный градиент от COLOR_START к COLOR_END."""
    img = Image.new('RGB', (size, size))
    pixels = img.load()

    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * size)
            t = min(1.0, max(0.0, t))
            r = int(COLOR_START[0] * (1 - t) + COLOR_END[0] * t)
            g = int(COLOR_START[1] * (1 - t) + COLOR_END[1] * t)
            b = int(COLOR_START[2] * (1 - t) + COLOR_END[2] * t)
            pixels[x, y] = (r, g, b)

    return img

def create_rounded_mask(size):
    """Создать маску со скруглённым прямоугольником."""
    mask = Image.new('L', (size, size), 0)
    draw = ImageDraw.Draw(mask)
    radius = int(0.30 * size)
    draw.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)],
        radius=radius,
        fill=255
    )
    return mask

def draw_document(draw, size, offset_x, offset_y, stroke_width):
    """Нарисовать контур документа и строки текста."""
    s = size * 0.58 / 24  # масштаб иконки
    stroke_w = max(1, int(stroke_width))

    # Контур документа: (5,4) -> (15,4) -> (19,8) -> (19,20) -> (5,20) -> (5,4)
    outline = [
        (5 * s + offset_x, 4 * s + offset_y),
        (15 * s + offset_x, 4 * s + offset_y),
        (19 * s + offset_x, 8 * s + offset_y),
        (19 * s + offset_x, 20 * s + offset_y),
        (5 * s + offset_x, 20 * s + offset_y),
    ]

    # Рисуем контур как последовательность линий
    for i in range(len(outline)):
        p1 = outline[i]
        p2 = outline[(i + 1) % len(outline)]
        draw.line([p1, p2], fill=COLOR_DARK + (255,), width=stroke_w)

    # Линии текста: (9,11)-(15,11) и (9,15)-(13,15)
    draw.line(
        [(9 * s + offset_x, 11 * s + offset_y), (15 * s + offset_x, 11 * s + offset_y)],
        fill=COLOR_DARK + (255,),
        width=stroke_w
    )
    draw.line(
        [(9 * s + offset_x, 15 * s + offset_y), (13 * s + offset_x, 15 * s + offset_y)],
        fill=COLOR_DARK + (255,),
        width=stroke_w
    )

def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print("Создаю градиент...")
    grad = create_gradient(GRAD_SIZE)

    print(f"Масштабирую до {MASTER_SIZE}x{MASTER_SIZE}...")
    master = grad.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)

    print("Создаю маску скругления...")
    mask = create_rounded_mask(MASTER_SIZE)

    print("Применяю маску и рисую документ...")
    rgba = Image.new('RGBA', (MASTER_SIZE, MASTER_SIZE))
    rgba.paste(master, (0, 0))
    rgba.putalpha(mask)

    # Рисуем документ
    draw = ImageDraw.Draw(rgba)
    s = MASTER_SIZE * 0.58 / 24
    offset_x = (MASTER_SIZE - 24 * s) / 2
    offset_y = (MASTER_SIZE - 24 * s) / 2
    stroke_width = 2.4 * s

    draw_document(draw, MASTER_SIZE, offset_x, offset_y, stroke_width)

    # Генерируем размеры
    print("Генерирую файлы favicon...")

    # PNG с прозрачностью
    for filename, size in [
        ('favicon-16x16.png', 16),
        ('favicon-32x32.png', 32),
        ('android-chrome-192x192.png', 192),
        ('android-chrome-512x512.png', 512),
    ]:
        img = rgba.resize((size, size), Image.LANCZOS)
        path = f'static/{filename}'
        img.save(path)
        print(f"  {filename}")

    # Apple-touch-icon без прозрачности (iOS обработает скругления сам)
    apple_size = 180
    apple_grad = grad.resize((apple_size, apple_size), Image.LANCZOS)
    apple_grad.save('static/apple-touch-icon.png')
    print(f"  apple-touch-icon.png")

    # ICO файл (стандартный 32x32)
    ico_32 = rgba.resize((32, 32), Image.LANCZOS)
    ico_32.save('static/favicon.ico')
    print(f"  favicon.ico (32x32)")

    print("\n[OK] Все файлы сгенерированы в static/")

if __name__ == '__main__':
    main()
