import os
import re
import math
from typing import List, Dict, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter

CURRENCY = os.getenv("CURRENCY", "$")


def money(x: float) -> str:
    return f"{CURRENCY}{float(x):,.2f}"


def font(size: int, bold: bool = False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Arial Bold.ttf" if bold else "Arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt) -> Tuple[int, int]:
    box = draw.textbbox((0, 0), str(text), font=fnt)
    return box[2] - box[0], box[3] - box[1]


def clean_display_text(value: str) -> str:
    value = str(value or "").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", value)


def rounded(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def draw_text_fit(draw, xy, text, max_width, start_size, fill, bold=False, min_size=14):
    text = clean_display_text(text)
    size = start_size
    while size >= min_size:
        fnt = font(size, bold)
        if text_size(draw, text, fnt)[0] <= max_width:
            draw.text(xy, text, font=fnt, fill=fill)
            return size
        size -= 1

    fnt = font(min_size, bold)
    short = text
    while short and text_size(draw, short + "...", fnt)[0] > max_width:
        short = short[:-1]
    draw.text(xy, short.rstrip() + "...", font=fnt, fill=fill)
    return min_size


def wrap_lines(draw, text, max_width, size, bold=False, max_lines=2):
    text = clean_display_text(text)
    if not text:
        return []
    fnt = font(size, bold)
    words = text.split()
    lines = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if text_size(draw, trial, fnt)[0] <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines:
        used = " ".join(lines).replace("...", "").split()
        if len(used) < len(words):
            last = lines[-1]
            while last and text_size(draw, last + "...", fnt)[0] > max_width:
                last = last[:-1]
            lines[-1] = last.rstrip() + "..."
    return lines


def draw_wrapped(draw, xy, text, max_width, size, fill, bold=False, max_lines=2, line_gap=4):
    x, y = xy
    for line in wrap_lines(draw, text, max_width, size, bold, max_lines):
        draw.text((x, y), line, font=font(size, bold), fill=fill)
        y += size + line_gap
    return y


def display_bet_tag(value: str) -> str:
    raw = clean_display_text(value).upper()
    raw = re.sub(r"[^A-Z0-9 +_\-/]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    compact = raw.replace(" ", "")
    if compact in {"FREE", "FREEBET", "FREEBETS"}:
        return "FREE BET"
    if compact in {"SPECIAL", "SPECIALBET", "SPECIALBETS"}:
        return "SPECIAL BET"
    if compact in {"BOOSTED", "BOOSTEDODD", "BOOSTEDODDS", "ODDSBOOST", "BOOST"}:
        return "BOOSTED ODDS"
    if compact == "BET":
        return ""
    return raw[:28]


def is_premium_bet_tag(value: str) -> bool:
    return display_bet_tag(value).replace(" ", "") in {"FREEBET", "SPECIALBET", "SPECIALBETS", "BOOSTEDODDS"}


def leg_display_parts(leg: Dict) -> Tuple[str, str, str]:
    event = clean_display_text(leg.get("event", ""))
    selection = clean_display_text(leg.get("selection", ""))
    market = clean_display_text(leg.get("market", "Winner")) or "Winner"
    if not selection:
        selection = event or "Selection"
    if not event:
        event = selection

    # Sportsbooks often show selection as YES/NO for props.
    # Use the market text as the main title so the ticket never says only "Yes".
    if selection.strip().lower() in {"yes", "no"} and market and market.lower() not in {"winner", "odds"}:
        selection = market
        market = "Prop"

    same_event = event.lower() == selection.lower()
    same_market = market.lower() in {selection.lower(), event.lower(), ""}
    subtitle_bits = []
    if event and not same_event:
        subtitle_bits.append(event)
    if market and not same_market:
        subtitle_bits.append(market)
    subtitle = " • ".join(subtitle_bits) if subtitle_bits else (market if same_event else "")
    return selection, subtitle, market.upper() if market else "ODDS"


def detect_sport_slug_from_text(text: str) -> str:
    text = f" {str(text or '').lower()} "

    if any(w in text for w in [" tennis ", " atp ", " wta ", " aces", " total games", " game handicap", " set handicap", " set betting", "paolini", "sweeny", "dimitrov", "montgomery", "medvedev", "opelka"]):
        return "tennis"
    if any(w in text for w in [" cricket ", " ipl ", " t20 ", " odi ", " wicket", " runs", " innings", " over ", " overs "]):
        return "cricket"
    if any(w in text for w in [" basketball ", " nba ", " rebounds", " assists", " points ", " quarter"]):
        return "basketball"
    if any(w in text for w in [" baseball ", " mlb ", " strikeout", " home run", " hits", " tigers", " dragons"]):
        return "baseball"
    if any(w in text for w in [" volleyball ", " volley ", " total sets"]):
        return "volleyball"
    if any(w in text for w in [" boxing ", " boxer "]):
        return "boxing"
    if any(w in text for w in [" mma ", " ufc "]):
        return "mma"
    if any(w in text for w in [" badminton "]):
        return "badminton"
    if any(w in text for w in [" football ", " soccer ", " fifa ", " corner", " goals", " match winner", " threeway", " fc ", " united ", " city ", "netherlands", "spain", "belgium", "england", "france", "japan"]):
        return "football"

    # Heuristic: tennis names often look like "Surname, Firstname - Surname, Firstname".
    if re.search(r"[A-Z][a-z]+,\s*[A-Z][a-z]+.*\s[-–—]\s.*[A-Z][a-z]+,\s*[A-Z][a-z]+", str(text)):
        return "tennis"

    return "other"


def normalize_sport_slug(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soccer": "football",
        "fifa": "football",
        "sports": "other",
        "nfl": "american_football",
        "ufc": "mma",
    }
    raw = aliases.get(raw, raw)
    allowed = {"football", "tennis", "cricket", "basketball", "baseball", "volleyball", "boxing", "mma", "badminton", "american_football", "other"}
    return raw if raw in allowed else ""


def detect_leg_sport_slug(leg: Dict) -> str:
    saved = normalize_sport_slug(leg.get("sport", ""))
    if saved:
        return saved
    return detect_sport_slug_from_text(
        f"{leg.get('event', '')} {leg.get('selection', '')} {leg.get('market', '')}"
    )


def detect_sport(legs: List[Dict]) -> str:
    labels = {
        "football": "FOOTBALL",
        "tennis": "TENNIS",
        "cricket": "CRICKET",
        "basketball": "BASKETBALL",
        "baseball": "BASEBALL",
        "volleyball": "VOLLEYBALL",
        "boxing": "BOXING",
        "mma": "MMA",
        "badminton": "BADMINTON",
        "american_football": "AMERICAN FOOTBALL",
        "other": "SPORTS",
    }

    saved = [detect_leg_sport_slug(l) for l in (legs or [])]
    saved = [s for s in saved if s and s != "other"]
    if saved:
        unique = []
        for s in saved:
            if s not in unique:
                unique.append(s)
        if len(unique) == 1:
            return labels.get(unique[0], "SPORTS")
        return "MIXED SPORTS"

    text = " ".join([
        str(l.get("event", "")) + " " + str(l.get("selection", "")) + " " + str(l.get("market", ""))
        for l in legs
    ])
    slug = detect_sport_slug_from_text(text)
    return labels.get(slug, "SPORTS")


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
        "boxing": "boxing",
        "mma": "mma",
        "badminton": "badminton",
        "sports": "other",
        "other": "other",
    }
    name = aliases.get(sport, re.sub(r"[^a-z0-9_]+", "_", sport).strip("_") or "other")
    return f"{name}.png"


def paste_png_icon(base_img: Image.Image, box, icon_path: str) -> bool:
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


def draw_sport_icon_chip(draw, base_img: Image.Image, x: int, y: int, sport: str, color: str, size: int = 70):
    sport = str(sport or "other").lower().strip()
    bg_fill = "#0D1A26"
    border = "#2B4054"
    icon = "#B9CDDC"
    soft = "#8EA6B9"

    rounded(draw, (x, y, x + size, y + size), max(16, size // 4), bg_fill, border, 2)

    icon_dir = os.getenv("ICONS_DIR", "icons")
    filename = sport_icon_file(sport)
    candidates = [
        os.path.join(icon_dir, filename),
        os.path.join(icon_dir, filename.replace("_", "-")),
        os.path.join(icon_dir, "other.png"),
    ]

    if isinstance(base_img, Image.Image):
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                if paste_png_icon(base_img, (x + 12, y + 12, x + size - 12, y + size - 12), candidate):
                    return

    cx = x + size // 2
    cy = y + size // 2

    if sport in ["football", "soccer"]:
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), outline=icon, width=3)
        draw.polygon([(cx, cy - 7), (cx + 7, cy - 2), (cx + 4, cy + 7), (cx - 4, cy + 7), (cx - 7, cy - 2)], outline=icon)
        draw.line((cx - 17, cy, cx - 7, cy - 2), fill=icon, width=2)
        draw.line((cx + 17, cy, cx + 7, cy - 2), fill=icon, width=2)
    elif sport == "tennis":
        draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), fill=icon)
        draw.arc((cx - 21, cy - 18, cx + 1, cy + 18), 300, 60, fill=bg_fill, width=4)
        draw.arc((cx - 1, cy - 18, cx + 21, cy + 18), 120, 240, fill=bg_fill, width=4)
    elif sport == "basketball":
        draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline=icon, width=3)
        draw.arc((cx - 22, cy - 18, cx + 2, cy + 18), 290, 70, fill=icon, width=2)
        draw.arc((cx - 2, cy - 18, cx + 22, cy + 18), 110, 250, fill=icon, width=2)
        draw.line((cx - 18, cy, cx + 18, cy), fill=icon, width=2)
        draw.line((cx, cy - 18, cx, cy + 18), fill=icon, width=2)
    else:
        draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline=icon, width=3)
        draw.arc((cx - 21, cy - 18, cx + 2, cy + 18), 300, 60, fill=soft, width=2)
        draw.arc((cx - 2, cy - 18, cx + 21, cy + 18), 120, 240, fill=soft, width=2)


def hex_points(cx, cy, r):
    pts = []
    for i in range(6):
        a = math.radians(30 + 60 * i)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def draw_check(draw, x, y, size, color, width=5):
    draw.line((x, y + size * 0.55, x + size * 0.35, y + size * 0.9), fill=color, width=width)
    draw.line((x + size * 0.35, y + size * 0.9, x + size, y), fill=color, width=width)


def draw_calendar_icon(draw, x, y, color):
    rounded(draw, (x, y, x + 42, y + 42), 8, None, color, 3)
    draw.line((x, y + 13, x + 42, y + 13), fill=color, width=3)
    draw.line((x + 12, y - 5, x + 12, y + 8), fill=color, width=4)
    draw.line((x + 30, y - 5, x + 30, y + 8), fill=color, width=4)
    for dx in [11, 22, 33]:
        draw.ellipse((x + dx - 2, y + 23, x + dx + 2, y + 27), fill=color)


def draw_clock_icon(draw, x, y, color):
    draw.ellipse((x, y, x + 44, y + 44), outline=color, width=4)
    draw.line((x + 22, y + 22, x + 22, y + 10), fill=color, width=4)
    draw.line((x + 22, y + 22, x + 33, y + 28), fill=color, width=4)


def draw_money_icon(draw, x, y, color):
    draw.ellipse((x, y, x + 46, y + 46), outline=color, width=4)
    draw.text((x + 13, y + 6), "$", fill=color, font=font(26, True))


def create_betslip_image(
    ticket_id: int,
    stake: float,
    total_odds: float,
    legs: List[Dict],
    conditions: List[str],
    created_at: str,
    bet_tag: str = "",
) -> str:
    legs = legs or []
    if not legs:
        legs = [{"event": "Selection", "selection": "Selection", "market": "Winner", "odds": total_odds}]

    W = 1024
    outer = 54
    inner_x = 92
    content_w = W - inner_x * 2
    leg_h = 154
    H = 1210 + (len(legs) * leg_h)
    H = max(H, 1380)  # exact dynamic height so footer/status never get clipped

    premium = is_premium_bet_tag(bet_tag)

    # Default Lenny dark-blue theme.
    bg = "#03070E"
    card = "#050912"
    panel = "#07101D"
    panel2 = "#061324"
    leg_card = "#07111D"
    border = "#184C80"
    border_soft = "#1A2C42"
    white = "#F5F7FB"
    muted = "#A2A9B8"
    blue = "#3588FF"
    green = "#56E777"
    gold = "#FDBB3E"
    orange = "#FFBA31"

    if premium:
        # FREE BET / SPECIAL BET look: black + antique gold, no loud side glow.
        bg = "#060401"
        card = "#0C0802"
        panel = "#120D05"
        panel2 = "#171006"
        leg_card = "#100B04"
        border = "#B8862D"
        border_soft = "#4A3513"
        white = "#FFF8DF"
        muted = "#C6B17A"
        blue = "#D8A441"       # reuse existing accent variable as premium gold
        gold = "#F0C35B"
        orange = "#E4AA38"

    img = Image.new("RGB", (W, H), bg)

    # Background ambience.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    if premium:
        # Low, centered golden halo. Avoid bright colored side contrast.
        gd.ellipse((W//2 - 520, -210, W//2 + 520, 530), fill=(210, 150, 45, 42))
        gd.ellipse((W//2 - 620, H - 560, W//2 + 620, H + 190), fill=(170, 112, 30, 28))
        gd.rectangle((0, 0, W, H), fill=(25, 16, 5, 22))
    else:
        # Big soft background glow matching the reference image.
        gd.ellipse((-260, -220, 560, 520), fill=(48, 132, 255, 48))
        gd.ellipse((500, -180, 1260, 520), fill=(75, 226, 117, 30))
        gd.ellipse((120, H - 350, 1120, H + 260), fill=(89, 65, 255, 28))
    glow = glow.filter(ImageFilter.GaussianBlur(82))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(img)

    if premium:
        # Subtle luxury texture: fine dark pinstripes + tiny gold dust.
        for yy in range(0, H, 18):
            draw.line((0, yy, W, yy), fill="#0D0903", width=1)
        for i in range(90):
            x = (i * 137) % W
            yy = (i * 251) % H
            a = 55 if i % 5 == 0 else 30
            draw.point((x, yy), fill=(190, 140, 50, a))

    # Outer glass card.
    if premium:
        rounded(draw, (outer, outer, W - outer, H - outer), 54, card, "#D9AA45", 3)
        rounded(draw, (outer + 7, outer + 7, W - outer - 7, H - outer - 7), 46, None, "#5A4218", 2)
        rounded(draw, (outer + 18, outer + 18, W - outer - 18, H - outer - 18), 34, None, "#241805", 1)
    else:
        rounded(draw, (outer, outer, W - outer, H - outer), 54, card, "#2A87FF", 2)
        rounded(draw, (outer + 5, outer + 5, W - outer - 5, H - outer - 5), 48, None, "#7361FF", 1)
        rounded(draw, (outer + 14, outer + 14, W - outer - 14, H - outer - 14), 38, None, "#162338", 1)

    y = 104

    # Header logo.
    cx, cy = inner_x + 68, y + 46
    draw.polygon(hex_points(cx, cy, 58), outline=gold, fill=None)
    draw.line(hex_points(cx, cy, 58) + [hex_points(cx, cy, 58)[0]], fill=gold, width=3)
    draw.text((cx, cy - 31), "LB", fill=gold, font=font(44, True), anchor="ma")

    draw.text((inner_x + 160, y + 4), "L E N N Y  B O O K", fill=gold, font=font(33, True))
    draw.text((inner_x + 160, y + 58), "PRIVATE TICKET", fill=muted, font=font(25))

    status_x = W - inner_x - 290
    draw.ellipse((status_x, y + 13, status_x + 40, y + 53), fill=green)
    draw_check(draw, status_x + 9, y + 23, 20, "#061319", width=5)
    draw.text((status_x + 55, y + 8), "BET ACCEPTED", fill=green, font=font(28, True))
    draw.text((status_x + 110, y + 58), f"TICKET #{int(ticket_id):05d}", fill=muted, font=font(24))

    draw.line((inner_x, y + 136, W - inner_x, y + 136), fill="#111B2A", width=2)

    y += 188
    draw.text((inner_x, y), "BETTING SLIP", fill=white, font=font(62, True))
    draw.line((inner_x, y + 90, inner_x + 182, y + 90), fill=gold, width=6)

    tag = display_bet_tag(bet_tag)
    if tag:
        bf = font(18, True)
        bw, bh = text_size(draw, tag, bf)
        bx2 = W - inner_x
        bx1 = bx2 - bw - 44
        tag_fill = "#211705" if premium else "#261B05"
        rounded(draw, (bx1, y + 16, bx2, y + 58), 16, tag_fill, gold, 2)
        draw.text((bx1 + 22, y + 26), tag, fill=gold, font=bf)

    y += 132

    # Summary panel.
    rounded(draw, (inner_x, y, W - inner_x, y + 168), 28, panel, border, 2)
    # inner glow line
    rounded(draw, (inner_x + 2, y + 2, W - inner_x - 2, y + 166), 26, None, "#3A2A10" if premium else "#0E2E51", 1)
    payout = round(float(stake) * float(total_odds), 2)
    col1 = inner_x + 42
    col2 = inner_x + 350
    col3 = inner_x + 620
    draw.line((inner_x + 295, y + 38, inner_x + 295, y + 130), fill=border_soft, width=2)
    draw.line((inner_x + 590, y + 38, inner_x + 590, y + 130), fill=border_soft, width=2)

    draw_money_icon(draw, col1, y + 54, gold)
    draw.text((col1 + 68, y + 52), "STAKE", fill=muted, font=font(22, True))
    draw.text((col1 + 68, y + 91), money(stake), fill=white, font=font(38, True))

    # bar chart icon
    ix = col2
    draw.rectangle((ix, y + 84, ix + 9, y + 112), outline=blue, width=3)
    draw.rectangle((ix + 17, y + 70, ix + 26, y + 112), outline=blue, width=3)
    draw.rectangle((ix + 34, y + 58, ix + 43, y + 112), outline=blue, width=3)
    draw.line((ix - 5, y + 116, ix + 55, y + 116), fill=blue, width=3)
    draw.line((ix + 7, y + 54, ix + 52, y + 18), fill=blue, width=4)
    draw.line((ix + 52, y + 18, ix + 52, y + 36), fill=blue, width=4)
    draw.line((ix + 52, y + 18, ix + 34, y + 18), fill=blue, width=4)
    draw.text((col2 + 80, y + 52), "ODDS", fill=muted, font=font(22, True))
    draw.text((col2 + 80, y + 91), f"{float(total_odds):.2f}x", fill=blue, font=font(38, True))

    draw_money_icon(draw, col3, y + 54, green)
    draw.text((col3 + 68, y + 52), "RETURN", fill=muted, font=font(22, True))
    draw_text_fit(draw, (col3 + 68, y + 91), money(payout), W - inner_x - (col3 + 68) - 10, 38, green, True, 18)

    y += 218

    # Sport/type row.
    sport = detect_sport(legs)
    draw_sport_icon_chip(draw, img, inner_x + 4, y - 7, detect_sport_slug_from_text(sport), blue, 56)
    draw.text((inner_x + 84, y + 5), sport, fill=blue, font=font(29, True))
    sw = text_size(draw, sport, font(29, True))[0]
    draw.line((inner_x + 105 + sw, y + 2, inner_x + 105 + sw, y + 43), fill=border_soft, width=2)
    
    is_sgm = any(
        "same game" in str(l.get("market", "")).lower() or
        "sgm" in str(l.get("market", "")).lower()
        for l in legs
    )

    if len(legs) == 1:
        type_text = "SINGLE BET"
    elif is_sgm:
        type_text = f"{len(legs)} LEG SAME GAME MULTI"
    else:
        type_text = f"{len(legs)} LEG PARLAY"
        
    draw.text((inner_x + 134 + sw, y + 5), type_text, fill=white, font=font(29, True))
    draw.line((W - inner_x - 450, y + 27, W - inner_x, y + 27), fill=border_soft, width=2)
    y += 82

    # Legs.
    for idx, leg in enumerate(legs, start=1):
        odds = float(leg.get("odds") or 0)
        title, subtitle, market_label = leg_display_parts(leg)
        rounded(draw, (inner_x, y, W - inner_x, y + 132), 26, leg_card, border, 2)
        rounded(draw, (inner_x + 2, y + 2, W - inner_x - 2, y + 130), 24, None, "#3A2A10" if premium else "#0C3158", 1)
        # leg sport icon
        leg_sport = detect_leg_sport_slug(leg)
        draw_sport_icon_chip(draw, img, inner_x + 35, y + 31, leg_sport, blue, 70)

        left_x = inner_x + 168
        right_div = W - inner_x - 228
        draw_text_fit(draw, (left_x, y + 26), title, right_div - left_x - 30, 34, white, True, 17)
        if subtitle:
            parts = subtitle.split(" • ")
            event_line = parts[0]
            draw_text_fit(draw, (left_x, y + 72), event_line, right_div - left_x - 30, 22, muted, False, 14)
            if len(parts) > 1:
                pill = "MARKET: " + parts[-1].upper()
                pf = font(19, True)
                pw = min(text_size(draw, pill, pf)[0] + 38, right_div - left_x - 30)
                pill_fill = "#1B1306" if premium else "#0B2039"
                pill_border = "#4A3513" if premium else "#122E50"
                pill_text = "#F0C35B" if premium else "#68A7FF"
                rounded(draw, (left_x, y + 101, left_x + pw, y + 132), 15, pill_fill, pill_border, 1)
                draw_text_fit(draw, (left_x + 19, y + 105), pill, pw - 30, 19, pill_text, True, 13)
        draw.line((right_div, y + 34, right_div, y + 102), fill=border_soft, width=2)
        odds_text = f"{odds:.2f}" if odds > 1 else "SGM"
        ow, _ = text_size(draw, odds_text, font(44, True))
        draw.text((W - inner_x - 72 - ow, y + 32), odds_text, fill=blue, font=font(44, True))
        label = "ODDS"
        lw, _ = text_size(draw, label, font(23, True))
        draw.text((W - inner_x - 72 - lw, y + 88), label, fill=muted, font=font(23, True))
        y += leg_h

    # Rule panel.
    condition_blob = " ".join([clean_display_text(c) for c in (conditions or [])])
    rule = "Any retirement or walkover = bet void."
    if condition_blob:
        if "retirement" in condition_blob.lower() or "walkover" in condition_blob.lower():
            rule = "Any retirement or walkover = bet void."
        else:
            first_rule = clean_display_text((conditions or [rule])[0])
            rule = rule if first_rule.upper().startswith("TEST BET") else first_rule

    rounded(draw, (inner_x, y, W - inner_x, y + 112), 24, "#061B16", "#105F45", 2)
    sx, sy = inner_x + 50, y + 24
    shield = [(sx + 32, sy), (sx + 66, sy + 14), (sx + 58, sy + 60), (sx + 32, sy + 78), (sx + 6, sy + 60), (sx - 2, sy + 14)]
    draw.line(shield + [shield[0]], fill=green, width=4)
    draw_check(draw, sx + 15, sy + 29, 32, green, width=4)
    draw.text((inner_x + 155, y + 29), "VOID RULE", fill=green, font=font(27, True))
    draw_text_fit(draw, (inner_x + 155, y + 68), rule, W - inner_x * 2 - 190, 25, muted, False, 16)
    y += 152

    # Date/time/status panel.
    rounded(draw, (inner_x, y, W - inner_x, y + 112), 22, panel, border_soft, 2)
    third = (W - inner_x * 2) // 3
    draw.line((inner_x + third, y + 25, inner_x + third, y + 87), fill=border_soft, width=2)
    draw.line((inner_x + third * 2, y + 25, inner_x + third * 2, y + 87), fill=border_soft, width=2)

    draw_calendar_icon(draw, inner_x + 38, y + 34, blue)
    draw.text((inner_x + 106, y + 32), "DATE", fill=muted, font=font(21, True))
    draw.text((inner_x + 106, y + 69), str(created_at or "")[:10], fill=white, font=font(25, True))

    draw_clock_icon(draw, inner_x + third + 46, y + 34, blue)
    draw.text((inner_x + third + 108, y + 32), "TIME", fill=muted, font=font(21, True))
    draw.text((inner_x + third + 108, y + 69), str(created_at or "")[11:16] if len(str(created_at or "")) >= 16 else "", fill=white, font=font(25, True))

    draw_money_icon(draw, inner_x + third * 2 + 44, y + 34, orange)
    draw.text((inner_x + third * 2 + 110, y + 32), "STATUS", fill=muted, font=font(21, True))
    draw.text((inner_x + third * 2 + 110, y + 68), "Pending", fill=orange, font=font(27, True))
    y += 152

    # Footer.
    rounded(draw, (inner_x, y, W - inner_x, y + 88), 20, panel, border_soft, 2)
    lx, ly = inner_x + 44, y + 24
    rounded(draw, (lx, ly + 22, lx + 38, ly + 58), 8, None, blue, 3)
    draw.arc((lx + 8, ly + 2, lx + 30, ly + 32), 180, 360, fill=blue, width=4)
    draw.text((inner_x + 110, y + 29), "VERIFIED PRIVATE TICKET", fill=blue, font=font(25, True))
    draw.line((W // 2 + 120, y + 18, W // 2 + 120, y + 70), fill=border_soft, width=2)
    draw.text((W - inner_x - 240, y + 29), "L E N N Y  B O O K", fill=gold, font=font(25, True))

    path = f"lenny_ticket_{ticket_id}.png"
    img.save(path, quality=96)
    return path