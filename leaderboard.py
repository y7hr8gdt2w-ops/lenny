import os
import re
from typing import List, Dict, Optional
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


CURRENCY = os.getenv("CURRENCY", "$")
AVATAR_DIR = os.getenv("AVATAR_DIR", "avatars")


def money(x: float) -> str:
    return f"{CURRENCY}{float(x):,.0f}"


def font(size: int, bold: bool = False):
    # LiberationSans looks cleaner for numbers, commas, $ and spacing.
    paths = [
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "Arial Bold.ttf" if bold else "Arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


def text_bbox(text: str, fnt):
    dummy = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(dummy)
    return d.textbbox((0, 0), str(text), font=fnt)


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt) -> tuple[int, int]:
    box = draw.textbbox((0, 0), str(text), font=fnt)
    return box[2] - box[0], box[3] - box[1]


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    min_size: int = 18,
    bold: bool = True,
):
    size = start_size

    while size >= min_size:
        fnt = font(size, bold)
        tw, _ = text_size(draw, text, fnt)

        if tw <= max_width:
            return fnt

        size -= 1

    return font(min_size, bold)


def draw_centered(draw: ImageDraw.ImageDraw, box, text: str, fnt, fill):
    x1, y1, x2, y2 = box
    tb = draw.textbbox((0, 0), str(text), font=fnt)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]

    tx = x1 + ((x2 - x1) - tw) / 2 - tb[0]
    ty = y1 + ((y2 - y1) - th) / 2 - tb[1]

    draw.text((tx, ty), str(text), font=fnt, fill=fill)


def draw_centered_stroked(draw: ImageDraw.ImageDraw, box, text: str, fnt, fill, stroke_fill="#000000", stroke_width: int = 2):
    x1, y1, x2, y2 = box
    tb = draw.textbbox((0, 0), str(text), font=fnt, stroke_width=stroke_width)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]

    tx = x1 + ((x2 - x1) - tw) / 2 - tb[0]
    ty = y1 + ((y2 - y1) - th) / 2 - tb[1]

    draw.text(
        (tx, ty),
        str(text),
        font=fnt,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def draw_dotted_vertical_line(
    img: Image.Image,
    x: int,
    y1: int,
    y2: int,
    color=(255, 190, 110, 190),
    dot_radius: int = 2,
    gap: int = 13,
):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    y = y1
    while y <= y2:
        d.ellipse(
            (x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius),
            fill=color,
        )
        y += gap

    img.alpha_composite(layer)


def clean_text(value: str) -> str:
    value = str(value or "").strip()
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value


def safe_username(value: str, fallback: str) -> str:
    value = clean_text(value)

    if value:
        return value

    return f"@{fallback}" if fallback else "@unknown"


def safe_rank(value) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None

        return int(value)
    except Exception:
        return None


def rounded(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def get_avatar_path(user_key: str, users_data: Dict) -> str:
    record = users_data.get("users", {}).get(user_key, {}) or {}
    avatar = clean_text(record.get("avatar", ""))

    if avatar:
        avatar = os.path.basename(avatar)
        path = os.path.join(AVATAR_DIR, avatar)

        if os.path.exists(path):
            return path

    default_path = os.path.join(AVATAR_DIR, "default.png")

    if os.path.exists(default_path):
        return default_path

    return ""


def load_avatar(path: str, size: int, radius: int = 34) -> Image.Image:
    if path and os.path.exists(path):
        try:
            img = Image.open(path).convert("RGB")
            img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS)
        except Exception:
            img = Image.new("RGB", (size, size), "#20242D")
    else:
        img = Image.new("RGB", (size, size), "#20242D")

    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (0, 0), mask)

    return out


def user_record(user_key: str, users_data: Dict) -> Dict:
    return users_data.get("users", {}).get(user_key, {}) or {}


def row_to_card_data(row: Dict, users_data: Dict) -> Dict:
    user_key = str(row.get("bettor") or "").strip().lower()
    rec = user_record(user_key, users_data)

    return {
        "key": user_key,
        "display": clean_text(rec.get("display", user_key.title())),
        "telegram": safe_username(rec.get("telegram", ""), user_key),
        "avatar_path": get_avatar_path(user_key, users_data),
        "wager": float(row.get("total_wager") or 0),
        "pnl": float(row.get("user_pnl") or 0),
        "total_bets": int(row.get("total_bets") or 0),
        "rank": safe_rank(row.get("rank")),
    }


def create_vertical_gradient(size, top_color, bottom_color) -> Image.Image:
    w, h = size
    gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = gradient.load()

    if h <= 1:
        for x in range(w):
            px[x, 0] = tuple(top_color)
        return gradient

    for y in range(h):
        t = y / (h - 1)

        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        a = int(top_color[3] + (bottom_color[3] - top_color[3]) * t)

        for x in range(w):
            px[x, y] = (r, g, b, a)

    return gradient


def draw_gradient_text(
    img: Image.Image,
    pos,
    text: str,
    fnt,
    top_color,
    bottom_color,
    shadow=(0, 0, 0, 150),
    shadow_offset=(2, 2),
    stroke_fill=(0, 0, 0, 160),
    stroke_width: int = 1,
):
    if not text:
        return

    bbox = text_bbox(text, fnt)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    tx = int(pos[0])
    ty = int(pos[1])

    pad = max(8, stroke_width * 5)

    # Shadow / outline layer
    shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.text(
        (tx - bbox[0] + shadow_offset[0], ty - bbox[1] + shadow_offset[1]),
        text,
        font=fnt,
        fill=shadow,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    img.alpha_composite(shadow_layer)

    # Text mask
    mask = Image.new("L", (tw + pad, th + pad), 0)
    md = ImageDraw.Draw(mask)
    md.text(
        (pad // 2 - bbox[0], pad // 2 - bbox[1]),
        text,
        font=fnt,
        fill=255,
        stroke_width=stroke_width,
        stroke_fill=255,
    )

    grad = create_vertical_gradient(mask.size, top_color, bottom_color)

    text_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    text_layer.paste(grad, (tx - pad // 2, ty - pad // 2), mask)
    img.alpha_composite(text_layer)


def theme_colors(theme: str):
    if theme == "gold":
        return {
            "fill": (235, 172, 32, 230),
            "outline": "#FFC247",
            "title_top": (255, 252, 215, 255),
            "title_bottom": (255, 186, 28, 255),
            "stat_fill": "#FFF7D0",
            "stat_stroke": "#000000",
        }

    if theme == "silver":
        return {
            "fill": (220, 220, 225, 225),
            "outline": "#F1F1F1",
            "title_top": (255, 255, 255, 255),
            "title_bottom": (175, 182, 195, 255),
            "stat_fill": "#F7F7F7",
            "stat_stroke": "#000000",
        }

    if theme == "bronze":
        return {
            "fill": (165, 70, 18, 225),
            "outline": "#D96A22",
            "title_top": (255, 230, 202, 255),
            "title_bottom": (210, 112, 38, 255),
            "stat_fill": "#FFF0DD",
            "stat_stroke": "#000000",
        }

    return {
        "fill": (70, 62, 58, 220),
        "outline": "#8A6A55",
        "title_top": (255, 255, 255, 255),
        "title_bottom": (226, 209, 191, 255),
        "stat_fill": "#FFF1E3",
        "stat_stroke": "#000000",
    }


def draw_heading(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    rank_label: str,
    data: Dict,
    theme: str,
):
    colors = theme_colors(theme)

    if theme == "you" and data.get("rank"):
        heading = f"You  #{data['rank']}"
    else:
        heading = rank_label

    fnt = font(58, True)

    draw_gradient_text(
        img,
        (x + 12, y - 72),
        heading,
        fnt,
        colors["title_top"],
        colors["title_bottom"],
        shadow=(0, 0, 0, 175),
        shadow_offset=(2, 2),
        stroke_fill=(0, 0, 0, 170),
        stroke_width=1,
    )


def format_pnl(pnl: float) -> str:
    pnl = float(pnl or 0)

    if pnl > 0:
        return f"Profit - {money(pnl)}"

    if pnl < 0:
        return f"Loss - {money(abs(pnl))}"

    return f"P/L - {money(0)}"


def draw_stats_plain(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    data: Dict,
    theme: str,
):
    colors = theme_colors(theme)

    wager_text = f"Wager - {money(data.get('wager', 0))}"
    pnl_text = format_pnl(data.get("pnl", 0))

    max_width = w - 52

    # Slightly smaller and cleaner font so spaces/commas stay visible.
    wager_font = fit_font(draw, wager_text, max_width=max_width, start_size=29, min_size=21, bold=True)
    pnl_font = fit_font(draw, pnl_text, max_width=max_width, start_size=29, min_size=21, bold=True)

    stat_y = y + h - 116

    draw_centered_stroked(
        draw,
        (x + 18, stat_y, x + w - 18, stat_y + 42),
        wager_text,
        wager_font,
        colors["stat_fill"],
        stroke_fill=colors["stat_stroke"],
        stroke_width=2,
    )

    draw_centered_stroked(
        draw,
        (x + 18, stat_y + 52, x + w - 18, stat_y + 94),
        pnl_text,
        pnl_font,
        colors["stat_fill"],
        stroke_fill=colors["stat_stroke"],
        stroke_width=2,
    )


def draw_avatar_card(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    rank_label: str,
    data: Dict,
    theme: str,
):
    colors = theme_colors(theme)

    draw_heading(img, draw, x, y, rank_label, data, theme)

    # Card shadow
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (x + 8, y + 12, x + w + 8, y + h + 12),
        radius=48,
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img.alpha_composite(shadow)

    # Card body
    card_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(card_layer)
    cd.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=52,
        fill=colors["fill"],
        outline=colors["outline"],
        width=3,
    )
    img.alpha_composite(card_layer)

    # Avatar
    avatar_size = int(w * 0.62)
    avatar_x = x + (w - avatar_size) // 2
    avatar_y = y + 55

    avatar = load_avatar(data.get("avatar_path", ""), avatar_size, radius=35)
    img.alpha_composite(avatar, (avatar_x, avatar_y))

    # Username pill
    username = clean_text(data.get("telegram", ""))
    username_font = fit_font(draw, username, max_width=w - 80, start_size=27, min_size=18, bold=True)

    pill_h = 48
    pill_w = min(w - 44, max(170, text_size(draw, username, username_font)[0] + 46))
    pill_x = x + (w - pill_w) // 2
    pill_y = avatar_y + avatar_size - 60

    rounded(draw, (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h), 24, "#00000088")
    draw_centered(
        draw,
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        username,
        username_font,
        "white",
    )

    # Wager / PnL text
    draw_stats_plain(draw, x, y, w, h, data, theme)


def create_monthly_leaderboard_image(
    leaderboard_rows: List[Dict],
    users_data: Dict,
    title: str,
    date_range_label: str,
    requester_row: Optional[Dict] = None,
) -> str:
    W, H = 1920, 1080

    bg = Image.new("RGBA", (W, H), "#120805")
    draw = ImageDraw.Draw(bg)

    # Background glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-300, -200, 800, 900), fill=(0, 0, 0, 120))
    gd.ellipse((700, -250, 2200, 1300), fill=(180, 70, 20, 125))
    gd.ellipse((300, 300, 1400, 1250), fill=(255, 170, 40, 45))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    bg.alpha_composite(glow)

    draw = ImageDraw.Draw(bg)

    # Header
    rounded(draw, (365, -70, 1555, 345), 90, "#1A1210DD", "#FF9B4A", 5)
    draw_centered(draw, (365, 110, 1555, 210), title, font(84, True), "white")
    draw_centered(draw, (365, 230, 1555, 290), date_range_label, font(40, False), "white")

    # Top cards
    cards = [row_to_card_data(dict(r), users_data) for r in leaderboard_rows[:3]]

    while len(cards) < 3:
        cards.append(
            {
                "key": "",
                "display": "",
                "telegram": "@empty",
                "avatar_path": get_avatar_path("", users_data),
                "wager": 0,
                "pnl": 0,
                "total_bets": 0,
                "rank": None,
            }
        )

    draw_avatar_card(bg, draw, 540, 500, 460, 500, "#1", cards[0], "gold")
    draw_avatar_card(bg, draw, 115, 575, 405, 425, "#2", cards[1], "silver")
    draw_avatar_card(bg, draw, 1025, 575, 405, 425, "#3", cards[2], "bronze")

    # Divider before You card
    draw_dotted_vertical_line(
        bg,
        x=1450,
        y1=520,
        y2=1000,
        color=(255, 190, 110, 190),
        dot_radius=2,
        gap=13,
    )

    draw = ImageDraw.Draw(bg)

    # You card
    if requester_row:
        you_data = row_to_card_data(dict(requester_row), users_data)
    else:
        you_data = {
            "key": "",
            "display": "You",
            "telegram": "@you",
            "avatar_path": get_avatar_path("", users_data),
            "wager": 0,
            "pnl": 0,
            "total_bets": 0,
            "rank": None,
        }

    draw_avatar_card(bg, draw, 1485, 625, 340, 375, "You", you_data, "you")

    path = "monthly_leaderboard.png"
    bg.convert("RGB").save(path, quality=95)

    return path