import os
import json
import math

import textwrap
import re
from datetime import datetime
from typing import Dict, List, Tuple, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


CURRENCY = os.getenv("CURRENCY", "$")
USERS_FILE = os.getenv("USERS_FILE", "users.json")
AVATAR_DIR = os.getenv("AVATAR_DIR", "avatars")


def money(x: float) -> str:
    return f"{CURRENCY}{float(x):,.2f}"


def font(size: int, bold: bool = False):
    paths = []

    if bold:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "arialbd.ttf",
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "arial.ttf",
        ]

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt) -> Tuple[int, int]:
    box = draw.textbbox((0, 0), str(text), font=fnt)
    return box[2] - box[0], box[3] - box[1]


def rounded(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    radius: int,
    fill: str,
    outline: str = None,
    width: int = 1,
):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text_fit(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    max_width: int,
    size: int,
    fill: str,
    bold: bool = False,
    min_size: int = 11,
):
    text = str(text or "")

    while size >= min_size:
        fnt = font(size, bold)
        w, _ = text_size(draw, text, fnt)

        if w <= max_width:
            draw.text(xy, text, fill=fill, font=fnt)
            return

        size -= 1

    fnt = font(min_size, bold)
    clipped = text

    while clipped and text_size(draw, clipped + "...", fnt)[0] > max_width:
        clipped = clipped[:-1]

    draw.text(xy, clipped + "...", fill=fill, font=fnt)


def wrap_text(draw, text: str, max_width: int, size: int, bold: bool = False) -> List[str]:
    text = str(text or "").strip()

    if not text:
        return []

    fnt = font(size, bold)
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()

        if text_size(draw, test, fnt)[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    max_width: int,
    size: int,
    fill: str,
    bold: bool = False,
    line_gap: int = 6,
    max_lines: int = None,
) -> int:
    fnt = font(size, bold)
    lines = wrap_text(draw, text, max_width, size, bold)

    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "..."

    x, y = xy

    for line in lines:
        draw.text((x, y), line, fill=fill, font=fnt)
        y += size + line_gap

    return y


def normalize_user_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "", str(value or "").strip().lower())


def normalize_telegram(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    value = value.replace("https://t.me/", "").replace("t.me/", "").strip()

    if value.startswith("@"):
        value = value[1:]

    value = re.sub(r"[^a-zA-Z0-9_]", "", value)
    return f"@{value}" if value else ""


def load_users_json() -> Dict:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass

    return {"users": {}}


def find_avatar_path(data: Dict) -> str:
    direct = str(data.get("avatar_path") or "").strip()
    if direct and os.path.exists(direct):
        return direct

    avatar = str(data.get("avatar") or "").strip()
    if avatar:
        p = os.path.join(AVATAR_DIR, avatar)
        if os.path.exists(p):
            return p
        if os.path.exists(avatar):
            return avatar

    users = load_users_json().get("users", {}) or {}

    candidates = []
    for key_name in ["bettor", "user_key", "key", "username", "name"]:
        raw = str(data.get(key_name) or "").strip()
        if raw:
            candidates.append(normalize_user_key(raw.replace("@", "")))

    telegram = normalize_telegram(data.get("telegram") or data.get("name") or "")

    for key in candidates:
        record = users.get(key, {}) or {}
        av = str(record.get("avatar") or "").strip()
        if av:
            p = os.path.join(AVATAR_DIR, av)
            if os.path.exists(p):
                return p

    if telegram:
        for key, record in users.items():
            tg = normalize_telegram(record.get("telegram") or "")
            if tg and tg.lower() == telegram.lower():
                av = str(record.get("avatar") or "").strip()
                if av:
                    p = os.path.join(AVATAR_DIR, av)
                    if os.path.exists(p):
                        return p

    for key in candidates:
        for ext in [".png", ".jpg", ".jpeg", ".webp"]:
            p = os.path.join(AVATAR_DIR, key + ext)
            if os.path.exists(p):
                return p

    for default in ["default.png", "default.jpg", "default.jpeg", "default.webp"]:
        p = os.path.join(AVATAR_DIR, default)
        if os.path.exists(p):
            return p

    return ""


def make_avatar(path: str, size: int, radius: int = None) -> Image.Image:
    radius = radius if radius is not None else size // 2

    if path and os.path.exists(path):
        try:
            avatar = Image.open(path).convert("RGBA")
            avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
        except Exception:
            avatar = Image.new("RGBA", (size, size), "#202635")
    else:
        avatar = Image.new("RGBA", (size, size), "#202635")
        ad = ImageDraw.Draw(avatar)
        ad.ellipse((size * 0.32, size * 0.18, size * 0.68, size * 0.54), fill="#E8EDF5")
        ad.rounded_rectangle(
            (size * 0.22, size * 0.61, size * 0.78, size * 0.86),
            radius=size // 10,
            fill="#D7DCE4",
        )

    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(avatar, (0, 0), mask)
    return out


def paste_profile_avatar(base: Image.Image, box: Tuple[int, int, int, int], path: str, ring="#2E8DFF"):
    x1, y1, x2, y2 = box
    size = x2 - x1

    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((x1 - 18, y1 - 18, x2 + 18, y2 + 18), fill=(46, 141, 255, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(22))
    base.alpha_composite(glow)

    draw = ImageDraw.Draw(base)
    draw.ellipse((x1 - 8, y1 - 8, x2 + 8, y2 + 8), outline="#1F6FEB", width=5)
    draw.ellipse((x1 - 2, y1 - 2, x2 + 2, y2 + 2), outline=ring, width=3)

    avatar = make_avatar(path, size, radius=size // 2)
    base.alpha_composite(avatar, (x1, y1))



def _split_match_sides(match_name: str) -> List[str]:
    parts = re.split(r"\s+(?:vs\.?|v)\s+|\s*[-–—]\s*", str(match_name or "").strip(), flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def _looks_like_team_side(side: str) -> bool:
    low = f" {str(side or '').lower().strip()} "
    team_hints = [
        " fc ", " united ", " city ", " town ", " rovers ", " wanderers ", " county ",
        " athletic ", " atletico ", " inter ", " real ", " club ", " sporting ",
        " arsenal ", " chelsea ", " liverpool ", " everton ", " villa ", " tottenham ",
        " barcelona ", " madrid ", " juventus ", " milan ", " napoli ", " roma ",
        " bayern ", " dortmund ", " psg ", " benfica ", " porto ", " ajax ",
        " belgium ", " england ", " france ", " japan ", " spain ", " italy ",
        " germany ", " brazil ", " argentina ", " portugal ", " netherlands ",
    ]
    return any(h in low for h in team_hints)


def _looks_like_person_side(side: str) -> bool:
    side = str(side or "").strip()
    if not side or any(ch.isdigit() for ch in side):
        return False

    if _looks_like_team_side(side):
        return False

    parts = [p for p in re.split(r"[\s,]+", side) if p]
    if not 1 <= len(parts) <= 4:
        return False

    return all(p.replace(".", "").replace("-", "").isalpha() for p in parts)


def _looks_like_tennis_matchup(match_name: str) -> bool:
    sides = _split_match_sides(match_name)
    return len(sides) == 2 and _looks_like_person_side(sides[0]) and _looks_like_person_side(sides[1])


def detect_sport(match_name: str = "", bet_on: str = "", market_name: str = "") -> str:
    """
    Detect sport from saved leg text.
    cards.py cannot call external AI/web, so this uses match/selection/market names.
    """
    raw_match = str(match_name or "").strip()
    text = f" {raw_match} {bet_on} {market_name} ".lower()

    if any(x in text for x in [" cricket ", " t20 ", " odi ", " ipl ", " wicket", " runs", " innings", " overs "]):
        return "cricket"
    if any(x in text for x in [" basketball ", " nba ", " rebounds", " assists", " points ", " quarter "]):
        return "basketball"
    if any(x in text for x in [" baseball ", " mlb ", " home run", " strikeout", " pitcher "]):
        return "baseball"
    if any(x in text for x in [" volleyball ", " volley ", " total sets", " sets handicap"]):
        return "volleyball"
    if any(x in text for x in [" tennis ", " atp ", " wta ", " aces", " double faults", " total games", " games handicap", " set betting"]):
        return "tennis"
    if any(x in text for x in [" football ", " soccer ", " premier league", " la liga", " serie a", " bundesliga", " ligue 1", " goals", " corners", " cards", " match winner", " both teams to score"]):
        return "football"

    if _looks_like_tennis_matchup(raw_match):
        return "tennis"

    if _looks_like_team_side(raw_match):
        return "football"

    return "other"



def sport_icon_file(sport: str) -> str:
    sport = str(sport or "other").lower().strip()
    aliases = {
        "soccer": "football",
        "football": "football",
        "tennis": "tennis",
        "cricket": "cricket",
        "basketball": "basketball",
        "baseball": "baseball",
        "volleyball": "volleyball",
        "american football": "american_football",
        "mma": "mma",
        "boxing": "boxing",
        "badminton": "badminton",
        "other": "other",
    }
    name = aliases.get(sport, re.sub(r"[^a-z0-9_]+", "_", sport).strip("_") or "other")
    return f"{name}.png"


def paste_png_icon(
    base_img: Image.Image,
    box: Tuple[int, int, int, int],
    icon_path: str,
) -> bool:
    try:
        icon_img = Image.open(icon_path).convert("RGBA")
    except Exception:
        return False

    x1, y1, x2, y2 = box
    max_w = max(1, x2 - x1)
    max_h = max(1, y2 - y1)

    icon_img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)

    px = x1 + (max_w - icon_img.width) // 2
    py = y1 + (max_h - icon_img.height) // 2

    try:
        base_img.alpha_composite(icon_img, (px, py))
    except Exception:
        base_img.paste(icon_img, (px, py), icon_img)

    return True


def draw_sport_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, sport: str, accent: str):
    """
    Uses custom PNG icons first.

    Put icons beside your bot like this:
        icons/tennis.png
        icons/football.png
        icons/cricket.png
        icons/basketball.png
        icons/baseball.png
        icons/volleyball.png
        icons/boxing.png
        icons/mma.png
        icons/badminton.png
        icons/other.png

    You can override the folder with:
        ICONS_DIR=/full/path/to/icons
    """
    sport = str(sport or "other").lower().strip()

    bg_fill = "#0D1A26"
    border = "#2B4054"
    icon = "#B9CDDC"
    soft = "#8EA6B9"

    # Stake-style dark rounded chip.
    draw.rounded_rectangle(
        (cx - 31, cy - 31, cx + 31, cy + 31),
        radius=15,
        fill=bg_fill,
        outline=border,
        width=2,
    )

    icon_dir = os.getenv("ICONS_DIR", "icons")
    filename = sport_icon_file(sport)

    candidates = [
        os.path.join(icon_dir, filename),
        os.path.join(icon_dir, filename.replace("_", "-")),
        os.path.join(icon_dir, "other.png"),
    ]

    # Need the underlying image object so PNG alpha can be pasted.
    base_img = getattr(draw, "_image", None) or getattr(draw, "im", None)
    if isinstance(base_img, Image.Image):
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                if paste_png_icon(base_img, (cx - 22, cy - 22, cx + 22, cy + 22), candidate):
                    return

    # Fallback drawn icons if PNG files are missing.
    if sport in ["football", "soccer"]:
        draw.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), outline=icon, width=3)
        draw.polygon(
            [(cx, cy - 7), (cx + 7, cy - 2), (cx + 4, cy + 7), (cx - 4, cy + 7), (cx - 7, cy - 2)],
            outline=icon,
        )
        draw.line((cx - 16, cy, cx - 7, cy - 2), fill=icon, width=2)
        draw.line((cx + 16, cy, cx + 7, cy - 2), fill=icon, width=2)
        draw.line((cx - 10, cy + 13, cx - 4, cy + 7), fill=icon, width=2)
        draw.line((cx + 10, cy + 13, cx + 4, cy + 7), fill=icon, width=2)

    elif sport == "tennis":
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=icon)
        draw.arc((cx - 20, cy - 17, cx + 1, cy + 17), 300, 60, fill=bg_fill, width=4)
        draw.arc((cx - 1, cy - 17, cx + 20, cy + 17), 120, 240, fill=bg_fill, width=4)

    elif sport == "cricket":
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=icon)
        draw.arc((cx - 19, cy - 18, cx + 3, cy + 18), 300, 60, fill=bg_fill, width=3)
        draw.arc((cx - 3, cy - 18, cx + 19, cy + 18), 120, 240, fill=bg_fill, width=3)
        for yy in [-8, -3, 2, 7]:
            draw.line((cx - 9, cy + yy, cx - 4, cy + yy + 3), fill=bg_fill, width=2)
            draw.line((cx + 9, cy + yy, cx + 4, cy + yy + 3), fill=bg_fill, width=2)

    elif sport == "basketball":
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), outline=icon, width=3)
        draw.arc((cx - 21, cy - 17, cx + 2, cy + 17), 290, 70, fill=icon, width=2)
        draw.arc((cx - 2, cy - 17, cx + 21, cy + 17), 110, 250, fill=icon, width=2)
        draw.line((cx - 17, cy, cx + 17, cy), fill=icon, width=2)
        draw.line((cx, cy - 17, cx, cy + 17), fill=icon, width=2)

    elif sport == "baseball":
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=icon)
        draw.arc((cx - 20, cy - 18, cx + 2, cy + 18), 300, 60, fill=bg_fill, width=3)
        draw.arc((cx - 2, cy - 18, cx + 20, cy + 18), 120, 240, fill=bg_fill, width=3)

    elif sport == "volleyball":
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), outline=icon, width=3)
        draw.arc((cx - 19, cy - 19, cx + 19, cy + 19), 210, 330, fill=icon, width=2)
        draw.arc((cx - 19, cy - 19, cx + 19, cy + 19), 330, 90, fill=icon, width=2)
        draw.arc((cx - 19, cy - 19, cx + 19, cy + 19), 90, 210, fill=icon, width=2)

    else:
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), outline=icon, width=3)
        draw.arc((cx - 20, cy - 17, cx + 2, cy + 17), 300, 60, fill=soft, width=2)
        draw.arc((cx - 2, cy - 17, cx + 20, cy + 17), 120, 240, fill=soft, width=2)


def draw_sports_strip(draw: ImageDraw.ImageDraw, x: int, y: int):
    # Removed: settlement receipt should not show every sport icon.
    return


def _settlement_leg_parts(leg) -> Tuple[str, str, str, float]:
    event = str(row_value(leg, "event", "") if "row_value" in globals() else (leg.get("event", "") if isinstance(leg, dict) else leg["event"] if hasattr(leg, "keys") and "event" in leg.keys() else "") or "").strip()
    selection = str(row_value(leg, "selection", "") if "row_value" in globals() else (leg.get("selection", "") if isinstance(leg, dict) else leg["selection"] if hasattr(leg, "keys") and "selection" in leg.keys() else "") or "").strip()
    market = str(row_value(leg, "market", "Winner") if "row_value" in globals() else (leg.get("market", "Winner") if isinstance(leg, dict) else leg["market"] if hasattr(leg, "keys") and "market" in leg.keys() else "Winner") or "Winner").strip()
    try:
        odds = float(row_value(leg, "odds", 0) if "row_value" in globals() else (leg.get("odds", 0) if isinstance(leg, dict) else leg["odds"] if hasattr(leg, "keys") and "odds" in leg.keys() else 0))
    except Exception:
        odds = 0.0
    if not selection:
        selection = event or "Selection"
    if not event:
        event = selection
    return event, selection, market or "Winner", odds



def _settlement_leg_sport(leg) -> str:
    try:
        if "row_value" in globals():
            value = row_value(leg, "sport", "")
        elif isinstance(leg, dict):
            value = leg.get("sport", "")
        elif hasattr(leg, "keys") and "sport" in leg.keys():
            value = leg["sport"]
        else:
            value = ""
    except Exception:
        value = ""

    value = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soccer": "football",
        "fifa": "football",
        "sports": "other",
        "nfl": "american_football",
        "ufc": "mma",
    }
    value = aliases.get(value, value)
    allowed = {"football", "tennis", "cricket", "basketball", "baseball", "volleyball", "boxing", "mma", "badminton", "american_football", "other"}
    return value if value in allowed else ""


def _normalize_settlement_legs(legs) -> List[Dict]:
    out = []
    try:
        raw_legs = list(legs or [])
    except Exception:
        raw_legs = []
    for leg in raw_legs:
        event, selection, market, odds = _settlement_leg_parts(leg)
        leg_sport = _settlement_leg_sport(leg) or detect_sport(event, selection, market)
        out.append({"event": event, "selection": selection, "market": market, "sport": leg_sport, "odds": odds})
    return out


def create_settlement_image(
    ticket_id: int,
    bettor: str,
    result: str,
    stake: float,
    total_odds: float,
    payout: float,
    pnl: float,
    created_at: str,
    settled_at: str,
    match_name: str = "",
    bet_on: str = "",
    market_name: str = "",
    sport: str = "",
    legs=None,
) -> str:
    result = str(result).lower().strip()

    settlement_legs = _normalize_settlement_legs(legs)
    if settlement_legs:
        first_leg = settlement_legs[0]
        match_name = str(match_name or first_leg.get("event") or "Match not provided")
        bet_on = str(bet_on or first_leg.get("selection") or "Selection not provided")
        market_name = str(market_name or first_leg.get("market") or "Market")
    else:
        match_name = str(match_name or "Match not provided")
        bet_on = str(bet_on or "Selection not provided")
        market_name = str(market_name or "Market")

    sport = str(sport or (settlement_legs[0].get("sport") if settlement_legs else "") or detect_sport(match_name, bet_on, market_name))

    if result == "win":
        accent = "#56E77A"
        glow = (86, 231, 122, 48)
        status = "WIN"
        headline = "TICKET WON"
        subline = "Slip settled successfully. Profit paid out."
        pnl_label = "PROFIT"
        status_text = "PAID OUT"
        pnl_display = f"+{money(abs(pnl))}"
        payout_display = money(payout)
        status_box_fill = "#071F12"
        bg_tint = "#04130A"
        card = "#06120C"
        panel = "#0A1810"
        soft_panel = "#08140D"
    elif result == "loss":
        accent = "#FF4D6D"
        glow = (255, 77, 109, 50)
        status = "LOSS"
        headline = "TICKET LOST"
        subline = "Slip settled as a loss. Stake deducted."
        pnl_label = "LOSS"
        status_text = "SETTLED LOSS"
        pnl_display = f"-{money(abs(float(stake)))}"
        payout_display = money(payout)
        status_box_fill = "#210711"
        bg_tint = "#16040A"
        card = "#13050B"
        panel = "#190811"
        soft_panel = "#15070D"
    else:
        accent = "#FBBF24"
        glow = (251, 191, 36, 45)
        status = "VOID"
        headline = "TICKET VOIDED"
        subline = "Slip settled as void. Stake returned."
        pnl_label = "P/L"
        status_text = "VOIDED"
        pnl_display = money(0)
        payout_display = money(0)
        status_box_fill = "#231A05"
        bg_tint = "#120F04"
        card = "#120F04"
        panel = "#171305"
        soft_panel = "#120F05"

    visible_legs = settlement_legs[:10]
    show_summary_panel = False
    legs_panel_h = 0
    if visible_legs:
        legs_panel_h = 66 + len(visible_legs) * 78
        if len(settlement_legs) > len(visible_legs):
            legs_panel_h += 34

    W = 900
    base_H = 980 if show_summary_panel else 780
    H = base_H + legs_panel_h

    bg = "#04060D"
    white = "#F8FAFC"
    muted = "#B8C0CC"
    dim = "#7D8798"
    gold = "#E8B64A"
    blue = "#2E8DFF"

    img = Image.new("RGB", (W, H), bg)

    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.ellipse((-260, -190, 520, 440), fill=glow)
    gd.ellipse((470, -120, W + 240, 420), fill=(255, 255, 255, 12))
    gd.ellipse((100, H - 380, W + 200, H + 160), fill=glow)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(90))

    img = Image.alpha_composite(img.convert("RGBA"), glow_layer).convert("RGB")
    draw = ImageDraw.Draw(img)

    rounded(draw, (28, 28, W - 28, H - 28), 38, bg_tint, accent, 3)
    rounded(draw, (38, 38, W - 38, H - 38), 32, card, "#263247", 1)

    wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wm)
    wd.line((625, 270, 835, 92), fill=(255, 255, 255, 16), width=7)
    wd.line((650, 220, 820, 78), fill=(255, 255, 255, 12), width=5)
    wd.line((610, 295, 805, 130), fill=(255, 255, 255, 9), width=4)
    wm = wm.filter(ImageFilter.GaussianBlur(1.1))

    img = Image.alpha_composite(img.convert("RGBA"), wm).convert("RGB")
    draw = ImageDraw.Draw(img)

    rounded(draw, (75, 70, 130, 125), 15, "#0B1220", "#2A3548", 1)
    draw.text((87, 79), "LB", fill=gold, font=font(37, True))
    draw.line((150, 78, 150, 134), fill="#314052", width=2)
    draw.text((175, 76), "L E N N Y  B O O K", fill=white, font=font(29, True))
    draw.text((176, 118), "SETTLEMENT RECEIPT", fill=muted, font=font(19))

    rounded(draw, (630, 72, 825, 128), 18, status_box_fill, accent, 2)
    draw.ellipse((655, 92, 674, 111), fill=accent)
    draw.text((692, 84), status, fill=accent, font=font(27, True))

    ticket_text = f"#{ticket_id:05d}"
    tw, _ = text_size(draw, ticket_text, font(18))
    draw.text((825 - tw - 12, 136), ticket_text, fill=muted, font=font(18))

    draw.line((75, 170, W - 75, 170), fill="#334155", width=2)

    draw.text((75, 210), headline, fill=white, font=font(54, True))
    draw.text((78, 271), subline, fill=muted, font=font(22))
    draw.line((78, 307, 248, 307), fill=accent, width=5)

    if show_summary_panel:
        rounded(draw, (75, 345, W - 75, 520), 24, soft_panel, "#304056", 2)
        draw_sport_icon(draw, 120, 432, sport, accent)

        draw.text((160, 380), "MATCH", fill=muted, font=font(15, True))
        draw_wrapped_text(draw, (160, 408), match_name, 220, 16, white, True, 5, 2)
        draw.line((408, 384, 408, 482), fill="#3A4656", width=2)

        draw.text((445, 380), "BET ON", fill=muted, font=font(15, True))
        draw_wrapped_text(draw, (445, 408), bet_on, 230, 18, white, True, 5, 2)
        draw_wrapped_text(draw, (445, 458), market_name, 230, 15, dim, False, 4, 2)
        draw.line((700, 384, 700, 482), fill="#3A4656", width=2)

        draw.text((735, 380), "SPORT", fill=muted, font=font(15, True))
        sport_display = "Other Sport" if sport.lower() == "other" else sport.title()
        text_fit(draw, (735, 410), sport_display, 92, 18, accent, True, 12)

        y = 545
    else:
        y = 345

    if visible_legs:
        rounded(draw, (75, y, W - 75, y + legs_panel_h - 18), 24, "#07101A", "#304056", 2)
        draw.text((110, y + 24), "ALL LEGS", fill=muted, font=font(15, True))
        type_text = "SINGLE" if len(settlement_legs) == 1 else f"{len(settlement_legs)} LEG MULTI"
        text_fit(draw, (225, y + 20), type_text, 220, 19, blue, True, 12)
        yy = y + 60
        for idx, leg in enumerate(visible_legs, start=1):
            event, selection, market, odds = _settlement_leg_parts(leg)
            row_top = yy
            draw.line((105, row_top - 8, W - 105, row_top - 8), fill="#1B2B3D", width=1)
            draw_sport_icon(draw, 128, row_top + 28, leg.get("sport") or detect_sport(event, selection, market), accent)
            draw.text((170, row_top + 2), f"{idx:02d}", fill=blue, font=font(16, True))
            text_fit(draw, (210, row_top - 1), selection, 315, 21, white, True, 13)
            draw_wrapped_text(draw, (210, row_top + 29), event, 330, 14, muted, False, 3, 1)
            pill = str(market or "Winner").upper()
            text_fit(draw, (555, row_top + 10), pill, 155, 15, dim, True, 10)
            if odds and odds > 1:
                odds_text = f"{odds:.2f}x"
                ow, _ = text_size(draw, odds_text, font(20, True))
                draw.text((W - 110 - ow, row_top + 12), odds_text, fill=blue, font=font(20, True))
            yy += 78
        if len(settlement_legs) > len(visible_legs):
            draw.text((110, yy), f"+ {len(settlement_legs) - len(visible_legs)} more legs", fill=muted, font=font(16, True))
        y += legs_panel_h

    rounded(draw, (75, y, W - 75, y + 108), 24, "#080D16", accent, 3)
    draw.text((110, y + 32), "USER", fill=muted, font=font(15, True))
    text_fit(draw, (110, y + 62), bettor, 230, 30, white, True, 17)
    draw.line((382, y + 25, 382, y + 85), fill="#3A4656", width=2)
    draw.text((420, y + 32), pnl_label, fill=muted, font=font(15, True))
    text_fit(draw, (420, y + 58), pnl_display, 190, 36, accent, True, 17)
    draw.line((650, y + 25, 650, y + 85), fill="#3A4656", width=2)
    draw.text((690, y + 32), "STATUS", fill=muted, font=font(15, True))
    text_fit(draw, (690, y + 62), status_text, 155, 22, accent, True, 13)
    y += 145

    rounded(draw, (75, y, W - 75, y + 105), 24, panel, "#2D3748", 2)
    draw.text((110, y + 30), "STAKE", fill=muted, font=font(15, True))
    draw.text((110, y + 61), money(stake), fill=white, font=font(25, True))
    draw.line((290, y + 25, 290, y + 83), fill="#334155", width=2)
    draw.text((320, y + 30), "ODDS", fill=muted, font=font(15, True))
    draw.text((320, y + 61), f"{float(total_odds):.2f}x", fill=blue, font=font(25, True))
    draw.line((500, y + 25, 500, y + 83), fill="#334155", width=2)
    draw.text((530, y + 30), "PAYOUT", fill=muted, font=font(15, True))
    payout_color = accent if result == "win" else "#D7DCE4"
    draw.text((530, y + 61), payout_display, fill=payout_color, font=font(25, True))
    y += 150

    draw.line((75, y, W - 75, y), fill="#334155", width=2)
    draw.text((108, y + 40), "CREATED", fill=muted, font=font(15, True))
    draw.text((108, y + 70), str(created_at), fill=white, font=font(22, True))
    draw.text((520, y + 40), "SETTLED", fill=muted, font=font(15, True))
    draw.text((520, y + 70), str(settled_at), fill=white, font=font(22, True))

    path = f"settlement_{ticket_id}.png"
    img.save(path, quality=95)
    return path

def create_profile_image(data: Dict) -> str:
    W, H = 900, 1050

    bg = "#04060D"
    card = "#070B14"
    panel = "#0D1524"
    border = "#263247"
    white = "#F8FAFC"
    muted = "#AAB4C4"
    gold = "#E8B64A"
    blue = "#2E8DFF"
    green = "#56E77A"
    red = "#FF4D6D"

    img = Image.new("RGB", (W, H), bg)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -180, 520, 440), fill=(46, 141, 255, 42))
    gd.ellipse((450, 90, W + 200, H + 130), fill=(86, 231, 122, 24))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img.convert("RGBA"), glow)
    draw = ImageDraw.Draw(img)

    rounded(draw, (32, 32, W - 32, H - 32), 38, card, border, 2)

    draw.text((75, 78), "LB", fill=gold, font=font(48, True))
    draw.line((150, 82, 150, 134), fill="#334155", width=2)
    draw.text((175, 76), "L E N N Y  B O O K", fill=white, font=font(29, True))
    draw.text((176, 118), "PRIVATE PLAYER PROFILE", fill=muted, font=font(19))
    draw.line((75, 165, W - 75, 165), fill="#334155", width=2)

    name = str(data.get("name") or "Unknown")
    telegram = str(data.get("telegram") or "")

    avatar_path = find_avatar_path(data)
    paste_profile_avatar(img, (82, 205, 232, 355), avatar_path, ring=blue)
    draw = ImageDraw.Draw(img)

    draw.text((270, 220), "PLAYER", fill=blue, font=font(20, True))
    text_fit(draw, (270, 255), name, 560, 45, white, True, 24)

    if telegram:
        draw.text((273, 308), telegram, fill=muted, font=font(22))

    rank = str(data.get("rank") or "").strip()
    if rank:
        rounded(draw, (270, 342, 270 + min(500, max(170, len(rank) * 14 + 65)), 378), 15, "#111C2D", "#2E8DFF", 1)
        draw.text((287, 350), "RANK", fill=blue, font=font(13, True))
        text_fit(draw, (345, 348), rank, 405, 19, white, True, 12)

    total_bets = int(data.get("total_bets", 0))
    open_bets = int(data.get("open_bets", 0))
    total_wager = float(data.get("total_wager", 0))
    lifetime_pnl = float(data.get("lifetime_pnl", 0))
    won = int(data.get("won", 0))
    lost = int(data.get("lost", 0))
    avg_stake = float(data.get("avg_stake", 0))
    win_rate = float(data.get("win_rate", 0))
    roi = float(data.get("roi", 0))
    bookie_balance = float(data.get("bookie_balance", 0))
    total_free_bet_value = float(data.get("total_free_bet_value", 0))
    free_bets_available = float(data.get("free_bets_available", 0))

    pnl_color = green if lifetime_pnl >= 0 else red
    roi_color = green if roi >= 0 else red

    ledger_color = red if bookie_balance > 0 else green if bookie_balance < 0 else white
    ledger_label = "OWES BOOKIE" if bookie_balance > 0 else "BOOKIE OWES" if bookie_balance < 0 else "LEDGER"

    rounded(draw, (75, 385, W - 75, 490), 24, panel, "#2D3A52", 2)
    draw.text((105, 415), "LIFETIME P/L", fill=muted, font=font(14, True))
    text_fit(draw, (105, 445), money(lifetime_pnl), 170, 30, pnl_color, True, 15)

    draw.line((292, 410, 292, 468), fill="#334155", width=2)

    draw.text((320, 415), "TOTAL WAGER", fill=muted, font=font(14, True))
    text_fit(draw, (320, 445), money(total_wager), 160, 28, white, True, 14)

    draw.line((492, 410, 492, 468), fill="#334155", width=2)

    draw.text((520, 415), ledger_label, fill=muted, font=font(14, True))
    text_fit(draw, (520, 445), money(abs(bookie_balance)), 150, 28, ledger_color, True, 14)

    draw.line((690, 410, 690, 468), fill="#334155", width=2)

    draw.text((722, 415), "ROI", fill=muted, font=font(14, True))
    text_fit(draw, (722, 445), f"{roi:.1f}%", 100, 26, roi_color, True, 13)

    # SETTLED (= WON + LOST) and ROI (already in the summary bar above) are
    # left out here so every number on the card is distinct.
    activity_cards = [
        ("TOTAL", str(total_bets), blue),
        ("OPEN", str(open_bets), gold),
        ("WIN RATE", f"{win_rate:.1f}%", green),
        ("WON", str(won), green),
        ("LOST", str(lost), red),
        ("AVG STAKE", money(avg_stake), white),
    ]

    gap = 18
    box_h = 90

    draw.text((75, 525), "ACTIVITY", fill=blue, font=font(16, True))

    x0, y0 = 75, 558
    box_w3 = 238

    for i, (label, value, color) in enumerate(activity_cards):
        col = i % 3
        row = i // 3
        x = x0 + col * (box_w3 + gap)
        y = y0 + row * (box_h + gap)
        rounded(draw, (x, y, x + box_w3, y + box_h), 20, "#0A1220", "#263247", 1)
        draw.text((x + 18, y + 16), label, fill=muted, font=font(13, True))
        text_fit(draw, (x + 18, y + 46), value, box_w3 - 36, 26, color, True, 12)

    activity_bottom = y0 + 2 * (box_h + gap) - gap

    bonus_cards = [
        ("FREE BET VALUE", money(total_free_bet_value), gold),
        ("FREE BETS AVAILABLE", money(free_bets_available), blue),
    ]

    bonus_label_y = activity_bottom + 34
    draw.text((75, bonus_label_y), "BONUSES", fill=blue, font=font(16, True))

    bx0, by0 = 75, bonus_label_y + 33
    box_w2 = 366

    for i, (label, value, color) in enumerate(bonus_cards):
        x = bx0 + i * (box_w2 + gap)
        rounded(draw, (x, by0, x + box_w2, by0 + box_h), 20, "#0A1220", "#263247", 1)
        draw.text((x + 18, by0 + 16), label, fill=muted, font=font(13, True))
        text_fit(draw, (x + 18, by0 + 46), value, box_w2 - 36, 26, color, True, 12)

    footer_line_y = by0 + box_h + 40
    draw.line((75, footer_line_y, W - 75, footer_line_y), fill="#334155", width=2)
    draw.text((360, footer_line_y + 32), "L E N N Y  B O O K", fill=gold, font=font(18, True))

    safe_name = re.sub(r"[^a-zA-Z0-9_@-]", "_", name.replace(" ", "_"))
    path = f"profile_{safe_name}.png"
    img.convert("RGB").save(path, quality=95)
    return path


def create_stats_image(data: Dict) -> str:
    W, H = 900, 820

    bg = "#04060D"
    card = "#070B14"
    panel = "#0D1524"
    border = "#263247"
    white = "#F8FAFC"
    muted = "#AAB4C4"
    gold = "#E8B64A"
    blue = "#2E8DFF"
    green = "#56E77A"
    red = "#FF4D6D"

    img = Image.new("RGB", (W, H), bg)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -180, 520, 440), fill=(46, 141, 255, 42))
    gd.ellipse((450, 90, W + 200, H + 130), fill=(232, 182, 74, 24))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(img)

    rounded(draw, (32, 32, W - 32, H - 32), 38, card, border, 2)

    draw.text((75, 78), "LB", fill=gold, font=font(48, True))
    draw.line((150, 82, 150, 134), fill="#334155", width=2)
    draw.text((175, 76), "L E N N Y  B O O K", fill=white, font=font(29, True))
    draw.text((176, 118), "BOOK PERFORMANCE DASHBOARD", fill=muted, font=font(19))
    draw.line((75, 165, W - 75, 165), fill="#334155", width=2)

    total = int(data.get("total", 0))
    open_count = int(data.get("open_count", 0))
    settled = int(data.get("settled", 0))
    total_wager = float(data.get("total_wager", 0))
    user_pnl = float(data.get("user_pnl", 0))
    book_pnl = float(data.get("book_pnl", 0))
    open_liability = float(data.get("open_liability", 0))
    won = int(data.get("won", 0))
    lost = int(data.get("lost", 0))
    avg_ticket = float(data.get("avg_ticket", 0))
    win_rate = float(data.get("win_rate", 0))
    roi = float(data.get("roi", 0))
    total_ledger_balance = float(data.get("total_ledger_balance", 0))

    book_color = green if book_pnl >= 0 else red
    user_color = green if user_pnl >= 0 else red
    roi_color = green if roi >= 0 else red

    draw.text((75, 205), "BOOK P/L", fill=blue, font=font(20, True))
    text_fit(draw, (75, 240), money(book_pnl), 520, 54, book_color, True, 24)
    draw.text((78, 303), "Voided tickets excluded. Ledger shows current bookie balances.", fill=muted, font=font(19))

    ledger_color = green if total_ledger_balance > 0 else red if total_ledger_balance < 0 else white
    ledger_label = "LEDGER OWED" if total_ledger_balance >= 0 else "BOOKIE OWES"

    rounded(draw, (75, 350, W - 75, 455), 24, panel, "#2D3A52", 2)
    draw.text((105, 380), "TOTAL WAGER", fill=muted, font=font(14, True))
    text_fit(draw, (105, 410), money(total_wager), 155, 27, white, True, 13)

    draw.line((282, 375, 282, 433), fill="#334155", width=2)

    draw.text((310, 380), "OPEN LIABILITY", fill=muted, font=font(14, True))
    text_fit(draw, (310, 410), money(open_liability), 150, 27, gold, True, 13)

    draw.line((482, 375, 482, 433), fill="#334155", width=2)

    draw.text((510, 380), ledger_label, fill=muted, font=font(14, True))
    text_fit(draw, (510, 410), money(abs(total_ledger_balance)), 145, 27, ledger_color, True, 13)

    draw.line((670, 375, 670, 433), fill="#334155", width=2)

    draw.text((700, 380), "USER P/L", fill=muted, font=font(14, True))
    text_fit(draw, (700, 410), money(user_pnl), 120, 25, user_color, True, 12)

    cards = [
        ("TOTAL", str(total), blue),
        ("OPEN", str(open_count), gold),
        ("SETTLED", str(settled), green),
        ("WON", str(won), green),
        ("LOST", str(lost), red),
        ("AVG TICKET", money(avg_ticket), white),
        ("LEDGER", money(abs(total_ledger_balance)), ledger_color),
        ("BOOK ROI", f"{roi:.1f}%", roi_color),
    ]

    x0, y0 = 75, 490
    box_w, box_h = 175, 98
    gap = 18

    for i, (label, value, color) in enumerate(cards):
        col = i % 4
        row = i // 4
        x = x0 + col * (box_w + gap)
        y = y0 + row * (box_h + gap)
        rounded(draw, (x, y, x + box_w, y + box_h), 20, "#0A1220", "#263247", 1)
        draw.text((x + 18, y + 18), label, fill=muted, font=font(13, True))
        text_fit(draw, (x + 18, y + 50), value, box_w - 36, 25, color, True, 12)

    draw.line((75, 735, W - 75, 735), fill="#334155", width=2)
    draw.text((360, 762), "L E N N Y  B O O K", fill=gold, font=font(18, True))

    path = "lenny_book_stats.png"
    img.save(path, quality=95)
    return path


def create_market_image(
    market_id: int,
    title: str,
    description: str,
    odds: float,
    rules: str,
    created_at: str,
) -> str:
    clean_title = str(title or "")

    match_info = ""
    if "|||" in clean_title:
        clean_title, match_info = clean_title.split("|||", 1)
        clean_title = clean_title.strip()
        match_info = match_info.strip()

    match_lines = [m.strip() for m in re.split(r";|\n", match_info) if m.strip()]
    rule_lines = [r.strip().lstrip("•").strip() for r in str(rules or "").splitlines() if r.strip()]

    W = 900

    temp = Image.new("RGB", (W, 1200), "#000")
    temp_draw = ImageDraw.Draw(temp)

    title_lines = wrap_text(temp_draw, clean_title, 730, 46, True)
    desc_lines = wrap_text(temp_draw, description, 700, 34, True)

    match_h = 0
    for m in match_lines:
        match_h += 54

    rules_h = 0
    for r in rule_lines:
        rules_h += max(38, len(wrap_text(temp_draw, r, 690, 22)) * 30)

    H = 700 + len(title_lines) * 48 + len(desc_lines) * 42 + match_h + rules_h
    H = max(980, min(H, 1700))

    bg = "#04060D"
    card = "#070B14"
    panel = "#0D1524"
    dark_panel = "#0A1220"
    border = "#263247"
    white = "#F8FAFC"
    muted = "#AAB4C4"
    dim = "#7D8798"
    gold = "#E8B64A"
    blue = "#2E8DFF"
    green = "#56E77A"

    img = Image.new("RGB", (W, H), bg)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -180, 520, 440), fill=(46, 141, 255, 50))
    gd.ellipse((470, 150, W + 200, H + 180), fill=(86, 231, 122, 32))
    glow = glow.filter(ImageFilter.GaussianBlur(95))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(img)

    rounded(draw, (32, 32, W - 32, H - 32), 38, card, "#1F6FEB", 2)
    rounded(draw, (42, 42, W - 42, H - 42), 32, "#050914", border, 1)

    draw.text((75, 80), "LB", fill=gold, font=font(47, True))
    draw.line((150, 82, 150, 136), fill="#334155", width=2)
    draw.text((175, 76), "L E N N Y  B O O K", fill=white, font=font(29, True))
    draw.text((176, 118), "SPECIAL MARKET", fill=muted, font=font(19))

    rounded(draw, (650, 75, 812, 132), 18, "#071F12", green, 2)
    draw.text((682, 91), "OPEN", fill=green, font=font(26, True))
    draw.text((700, 138), f"#{market_id:05d}", fill=muted, font=font(18))

    draw.line((75, 182, W - 75, 182), fill="#334155", width=2)

    y = 230

    draw.text((75, y), "MARKET", fill=blue, font=font(20, True))
    y += 42

    for line in title_lines[:3]:
        draw.text((75, y), line, fill=white, font=font(46, True))
        y += 52

    y += 24

    if match_lines:
        rounded(draw, (75, y, W - 75, y + 72 + len(match_lines) * 50), 24, dark_panel, "#2C74D8", 2)
        draw.text((110, y + 24), "MATCH INFO", fill=muted, font=font(16, True))

        yy = y + 62
        for i, m in enumerate(match_lines[:12]):
            draw.ellipse((112, yy + 8, 124, yy + 20), fill=blue)
            text_fit(draw, (138, yy), m, 640, 24, white, True, 13)
            yy += 50

        y = yy + 30

    rounded(draw, (75, y, W - 75, y + 165), 24, panel, "#314158", 2)
    draw.text((110, y + 32), "SELECTION", fill=muted, font=font(16, True))
    yy = y + 68

    for line in desc_lines[:2]:
        draw.text((110, yy), line, fill=white, font=font(34, True))
        yy += 42

    y += 200

    rounded(draw, (75, y, W - 75, y + 120), 24, "#071426", "#1F6FEB", 2)

    draw.text((118, y + 35), "ODDS", fill=muted, font=font(16, True))
    draw.text((118, y + 64), f"{float(odds):.2f}x", fill=blue, font=font(36, True))

    draw.line((420, y + 32, 420, y + 92), fill="#334155", width=2)

    draw.text((470, y + 35), "BETTING", fill=muted, font=font(16, True))
    draw.text((470, y + 66), "Admin Approval Only", fill=green, font=font(25, True))

    y += 160

    if rule_lines:
        box_top = y
        box_h = 92 + min(len(rule_lines), 10) * 42
        rounded(draw, (75, box_top, W - 75, box_top + box_h), 24, panel, "#314158", 2)

        draw.text((110, box_top + 32), "RULES / CONDITIONS", fill=gold, font=font(18, True))

        yy = box_top + 70
        for r in rule_lines[:10]:
            lines = wrap_text(draw, r, 690, 22)

            for j, line in enumerate(lines[:2]):
                prefix = "• " if j == 0 else "  "
                draw.text((112, yy), prefix + line, fill=white, font=font(22))
                yy += 30

            yy += 6

        y = box_top + box_h + 35

    draw.line((170, H - 90, 360, H - 90), fill="#6B5D38", width=1)
    draw.text((370, H - 104), "L E N N Y  B O O K", fill=gold, font=font(18, True))
    draw.line((540, H - 90, 730, H - 90), fill="#6B5D38", width=1)

    path = f"market_{market_id}.png"
    img.save(path, quality=95)
    return path


def create_market_settlement_image(
    market_id: int,
    title: str,
    result: str,
    total_bets: int,
    total_staked: float,
    total_payout: float,
    book_pnl: float,
    settled_at: str,
) -> str:
    result = str(result).lower().strip()

    if result == "win":
        accent = "#56E77A"
        headline = "MARKET WON"
        status = "WIN"
        subline = "All market bets settled as winners."
        glow_color = (86, 231, 122, 42)
    elif result == "loss":
        accent = "#FF4D6D"
        headline = "MARKET LOST"
        status = "LOSS"
        subline = "All market bets settled as losses."
        glow_color = (255, 77, 109, 42)
    else:
        accent = "#FBBF24"
        headline = "MARKET VOID"
        status = "VOID"
        subline = "All market bets settled as void."
        glow_color = (251, 191, 36, 42)

    W, H = 900, 760
    bg = "#04060D"
    card = "#070B14"
    panel = "#0D1524"
    border = accent
    white = "#F8FAFC"
    muted = "#AAB4C4"
    gold = "#E8B64A"
    blue = "#2E8DFF"
    pnl_color = "#56E77A" if float(book_pnl) >= 0 else "#FF4D6D"

    img = Image.new("RGB", (W, H), bg)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -180, 520, 440), fill=glow_color)
    gd.ellipse((400, 180, W + 180, H + 160), fill=glow_color)
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(img)

    rounded(draw, (32, 32, W - 32, H - 32), 38, "#050914", border, 3)
    rounded(draw, (42, 42, W - 42, H - 42), 32, card, "#253044", 1)

    draw.text((75, 78), "LB", fill=gold, font=font(46, True))
    draw.line((150, 82, 150, 132), fill="#334155", width=2)
    draw.text((175, 76), "L E N N Y  B O O K", fill=white, font=font(29, True))
    draw.text((176, 118), "SPECIAL MARKET RECEIPT", fill=muted, font=font(19))

    rounded(draw, (630, 72, 825, 128), 18, "#071426", border, 2)
    draw.ellipse((655, 92, 674, 111), fill=accent)
    draw.text((692, 84), status, fill=accent, font=font(27, True))
    draw.text((740, 125), f"#{market_id:05d}", fill=muted, font=font(18))

    draw.line((75, 162, W - 75, 162), fill="#334155", width=2)

    draw.text((75, 220), headline, fill=white, font=font(52, True))
    draw.text((78, 280), subline, fill=muted, font=font(22))
    draw.line((78, 318, 248, 318), fill=accent, width=5)

    rounded(draw, (75, 360, W - 75, 465), 24, panel, border, 2)
    draw.text((110, 390), "MARKET", fill=muted, font=font(15, True))
    text_fit(draw, (110, 420), title, 420, 30, white, True, 14)

    draw.line((560, 385, 560, 442), fill="#334155", width=2)

    draw.text((600, 390), "BOOK P/L", fill=muted, font=font(15, True))
    draw.text((600, 420), money(book_pnl), fill=pnl_color, font=font(34, True))

    rounded(draw, (75, 505, W - 75, 610), 24, "#0A1220", "#2D3748", 2)

    draw.text((110, 535), "TOTAL BETS", fill=muted, font=font(15, True))
    draw.text((110, 566), str(total_bets), fill=white, font=font(25, True))

    draw.text((320, 535), "STAKED", fill=muted, font=font(15, True))
    draw.text((320, 566), money(total_staked), fill=blue, font=font(25, True))

    draw.text((525, 535), "PAYOUT", fill=muted, font=font(15, True))
    draw.text((525, 566), money(total_payout), fill=accent, font=font(25, True))

    draw.line((75, 650, W - 75, 650), fill="#334155", width=2)
    draw.text((108, 684), "SETTLED", fill=muted, font=font(15, True))
    draw.text((108, 714), str(settled_at), fill=white, font=font(22, True))

    draw.text((560, 714), "L E N N Y  B O O K", fill=gold, font=font(18, True))

    path = f"market_settlement_{market_id}.png"
    img.save(path, quality=95)
    return path
