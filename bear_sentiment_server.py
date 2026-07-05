#!/usr/bin/env python3
"""
Baby Bear NFT — BTC Sentiment Edition
──────────────────────────────────────────────────────────────────
20-minute BTC price change → background colour for ALL 10,000 bears

  UP  < 1 %  →  💜 Purple
  UP  1–2 %  →  🟡 Yellow
  UP  > 2 %  →  🌸 Pink
  DN  < 1 %  →  💙 Blue
  DN  ≥ 1 %  →  🖤 Black

Run:  python3 bear_sentiment_server.py
Open: http://localhost:8889
"""

import struct, zlib, os, json, random, time, threading, math, ssl
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error   import URLError

# SSL — try the system trust store first (works on Render), fall back to certifi.
# Certificate verification is never disabled: these calls fetch prices that drive
# the whole collection, so a MITM feeding fake candles would corrupt every bear.
def _make_ssl_ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return ssl.create_default_context()

_ssl_ctx = _make_ssl_ctx()

# ══════════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════════
PORT         = int(os.environ.get('PORT', 8889))
BASE_URL     = os.environ.get('BASE_URL', '').rstrip('/')  # e.g. https://baby-bear.railway.app
PRICE_TTL    = 300      # refresh BTC price every 5 min
W, H         = 24, 24
SCALE        = 20       # 480×480

META_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata")
ALL_META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_metadata.json")
PREVIEW_IDS  = [1,500,1000,1500,2000,2500,3000,3500,4000,4500,
                5000,5500,6000,6500,7000,7500,8000,8500,9000,9500]

# ── Sentiment thresholds ──────────────────────────────────────────
SENTIMENTS = [
    # (min_pct, max_pct, key,         bg_key,              emoji, label,          css_color)
    ( 2.0,  999, 'up_high',   'sent_pink',    '🌸', 'HOT BULL',    '#d63087'),
    ( 1.0,  2.0, 'up_mid',    'sent_yellow',  '🟡', 'BULLISH',     '#d4a800'),
    ( 0.0,  1.0, 'up_low',    'sent_purple',  '💜', 'SLIGHT UP',   '#7b3db8'),
    (-1.0,  0.0, 'down_low',  'sent_blue',    '💙', 'SLIGHT DOWN', '#1565c0'),
    (-999, -1.0, 'down_high', 'sent_black',   '🖤', 'BEARISH',     '#111118'),
]

SENT_COLORS = {
    'sent_pink':   (210,  48, 130, 255),
    'sent_yellow': (230, 180,   0, 255),
    'sent_purple': (115,  55, 175, 255),
    'sent_blue':   ( 20,  90, 185, 255),
    'sent_black':  ( 14,  14,  22, 255),
}

def pct_to_sentiment(pct):
    for mn, mx, key, bg_key, emoji, label, css in SENTIMENTS:
        if mn <= pct < mx:
            return key, bg_key, emoji, label, css
    return SENTIMENTS[-1][2:]   # fallback black

# ══════════════════════════════════════════════════════════════════
#  PNG writer
# ══════════════════════════════════════════════════════════════════
def _chunk(tag, data):
    body = tag + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

def bear_to_png(cv):
    nw = W * SCALE
    raw = bytearray()
    for y in range(H):
        row = bytearray()
        for x in range(W):
            i = (y*W+x)*4
            row += cv[i:i+4] * SCALE
        line = b'\x00' + bytes(row)
        for _ in range(SCALE): raw += line
    comp = zlib.compress(bytes(raw), 6)
    return bytes(b'\x89PNG\r\n\x1a\n'
                 + _chunk(b'IHDR', struct.pack('>IIBBBBB', nw, H*SCALE, 8, 6, 0, 0, 0))
                 + _chunk(b'IDAT', comp)
                 + _chunk(b'IEND', b''))

def _png_wide(cv, cw, ch, scale):
    nw, nh = cw * scale, ch * scale
    raw = bytearray()
    for y in range(ch):
        row = bytearray()
        for x in range(cw):
            i = (y * cw + x) * 4
            row += cv[i:i+4] * scale
        line = b'\x00' + bytes(row)
        for _ in range(scale): raw += line
    comp = zlib.compress(bytes(raw), 6)
    return bytes(b'\x89PNG\r\n\x1a\n'
                 + _chunk(b'IHDR', struct.pack('>IIBBBBB', nw, nh, 8, 6, 0, 0, 0))
                 + _chunk(b'IDAT', comp)
                 + _chunk(b'IEND', b''))

def build_banner_png():
    """1400×350 banner: 5 bears (one per sentiment color) on dark background."""
    BW, BH, BSCALE = 140, 35, 10
    bcv = bytearray(bytes((10, 10, 18, 255)) * (BW * BH))
    if not BEARS:
        return _png_wide(bcv, BW, BH, BSCALE)
    sent_keys = ['sent_black', 'sent_blue', 'sent_purple', 'sent_yellow', 'sent_pink']
    candidates = [1, 500, 1000, 2000, 5000]
    all_ids = list(BEARS.keys())
    tids = [c for c in candidates if c in BEARS]
    while len(tids) < 5:
        tids.append(all_ids[len(tids) % len(all_ids)])
    BY = (BH - H) // 2
    for i, (bg_key, tid) in enumerate(zip(sent_keys, tids)):
        bx = 5 + i * 27
        t = BEARS[tid]
        bear_cv = draw_bear(bg_key, t['fur'], t['eye'], t['mouth'],
                            t['cheek'], t['hat'], t['eyegear'], t['neck'])
        for y in range(H):
            for x in range(W):
                si = (y * W + x) * 4
                di = ((BY + y) * BW + (bx + x)) * 4
                bcv[di:di+4] = bear_cv[si:si+4]
    return _png_wide(bcv, BW, BH, BSCALE)

# ══════════════════════════════════════════════════════════════════
#  Canvas primitives  (flat RGBA bytearray W×H×4)
# ══════════════════════════════════════════════════════════════════
def make_cv(bg): return bytearray(bytes(bg) * (W*H))

def px(cv, x, y, c):
    if 0<=x<W and 0<=y<H:
        i=(y*W+x)*4; a=c[3]
        if a==255: cv[i],cv[i+1],cv[i+2]=c[0],c[1],c[2]
        elif a>0:
            f=a/255.; cv[i]=int(c[0]*f+cv[i]*(1-f)); cv[i+1]=int(c[1]*f+cv[i+1]*(1-f)); cv[i+2]=int(c[2]*f+cv[i+2]*(1-f))

def ell(cv, cx,cy,rx,ry,c):
    rx2,ry2=rx*rx,ry*ry
    for y in range(max(0,int(cy-ry)-1),min(H,int(cy+ry)+2)):
        for x in range(max(0,int(cx-rx)-1),min(W,int(cx+rx)+2)):
            if (x-cx)**2*ry2+(y-cy)**2*rx2<=rx2*ry2: px(cv,x,y,c)

def rct(cv, x1,y1,x2,y2,c):
    for y in range(max(0,y1),min(H,y2)):
        for x in range(max(0,x1),min(W,x2)): px(cv,x,y,c)

def ln(cv, x1,y1,x2,y2,c):
    dx,dy=abs(x2-x1),abs(y2-y1); sx=1 if x1<x2 else -1; sy=1 if y1<y2 else -1; err=dx-dy
    while True:
        px(cv,x1,y1,c)
        if x1==x2 and y1==y2: break
        e2=2*err
        if e2>-dy: err-=dy; x1+=sx
        if e2< dx: err+=dx; y1+=sy

def tri(cv, p1,p2,p3,c):
    pts=sorted([p1,p2,p3],key=lambda p:p[1]); (ax,ay),(bx,by),(cx2,cy2)=pts
    def lp(xa,ya,xb,yb,y): return float(xa) if ya==yb else xa+(xb-xa)*(y-ya)/(yb-ya)
    for y in range(max(0,ay),min(H,cy2+1)):
        xl=lp(ax,ay,bx,by,y) if y<=by else lp(bx,by,cx2,cy2,y); xr=lp(ax,ay,cx2,cy2,y)
        for x in range(max(0,int(min(xl,xr))),min(W,int(max(xl,xr))+1)): px(cv,x,y,c)

def heart_shape(cv, cx,cy,sz,c):
    r=max(1,sz//2+1); ell(cv,cx-r+1,cy-r//2,r,r,c); ell(cv,cx+r-1,cy-r//2,r,r,c)
    tri(cv,(cx-sz,cy+r//2-1),(cx+sz,cy+r//2-1),(cx,cy+sz),c)

# ══════════════════════════════════════════════════════════════════
#  Palettes  (original + sentiment injected)
# ══════════════════════════════════════════════════════════════════
BACKGROUNDS = {
    "vivid_orange":(255,140,0,255),"cobalt_blue":(30,120,220,255),
    "royal_purple":(120,50,200,255),"emerald":(0,165,95,255),
    "crimson":(210,35,55,255),"golden":(240,195,0,255),
    "hot_pink":(235,80,155,255),"teal":(0,175,175,255),
    "midnight":(18,20,42,255),"electric_lime":(160,230,0,255),
    "sky_blue":(85,185,255,255),"warm_white":(245,243,238,255),
    **SENT_COLORS,   # inject sentiment colours
}

FUR = {
    "honey_brown":  dict(o=(82,45,12,255),  m=(148,92,40,255),  l=(192,138,80,255),  f=(215,175,130,255)),
    "midnight_blk": dict(o=(18,16,24,255),  m=(52,48,60,255),   l=(88,84,98,255),    f=(120,115,135,255)),
    "polar_white":  dict(o=(170,175,185,255),m=(220,224,232,255),l=(242,244,248,255), f=(252,253,255,255)),
    "golden_bear":  dict(o=(165,115,15,255), m=(215,168,45,255), l=(242,208,105,255), f=(252,232,165,255)),
    "steel_gray":   dict(o=(75,75,88,255),  m=(142,142,155,255),l=(195,195,208,255),  f=(222,222,232,255)),
    "rose_pink":    dict(o=(175,92,125,255), m=(225,155,178,255),l=(248,202,220,255),  f=(252,228,240,255)),
    "sky_blue_fur": dict(o=(52,72,158,255),  m=(105,138,210,255),l=(158,192,242,255),  f=(195,218,252,255)),
    "lavender_fur": dict(o=(95,55,148,255),  m=(155,115,208,255),l=(202,172,238,255),  f=(228,210,250,255)),
    "zombie_grn":   dict(o=(38,78,35,255),   m=(82,138,72,255),  l=(125,178,115,255),  f=(162,208,152,255)),
    "alien_teal":   dict(o=(40,115,155,255), m=(88,182,208,255), l=(145,215,232,255),  f=(188,235,245,255)),
}

# ══════════════════════════════════════════════════════════════════
#  Trait renderers (same as baby_bear_10k.py)
# ══════════════════════════════════════════════════════════════════
EL,ER,EY=8,16,10
EY_BK=(22,18,28,255);EY_SH=(255,255,255,200);EY_RD=(220,40,40,255)
EY_PK=(210,80,130,255);EY_YL=(248,210,30,255);GLD=(218,168,48,255)

def draw_eyes(cv,style,o):
    if style=="normal":
        for ex in(EL,ER): ell(cv,ex,EY,2,2,EY_BK); px(cv,ex-1,EY-1,EY_SH)
    elif style=="closed":
        for ex in(EL,ER): px(cv,ex-1,EY,EY_BK);px(cv,ex,EY-1,EY_BK);px(cv,ex+1,EY,EY_BK)
    elif style=="wink_r":
        ell(cv,EL,EY,2,2,EY_BK);px(cv,EL-1,EY-1,EY_SH)
        px(cv,ER-1,EY,EY_BK);px(cv,ER,EY-1,EY_BK);px(cv,ER+1,EY,EY_BK)
    elif style=="wink_l":
        ell(cv,ER,EY,2,2,EY_BK);px(cv,ER-1,EY-1,EY_SH)
        px(cv,EL-1,EY,EY_BK);px(cv,EL,EY-1,EY_BK);px(cv,EL+1,EY,EY_BK)
    elif style=="heart":
        heart_shape(cv,EL,EY,2,EY_PK);heart_shape(cv,ER,EY,2,EY_PK)
    elif style=="star":
        for ex in(EL,ER):
            for dp in[(-1,0),(1,0),(0,-1),(0,1),(0,0)]: px(cv,ex+dp[0],EY+dp[1],EY_YL)
    elif style=="angry":
        for ex in(EL,ER): ell(cv,ex,EY,2,2,EY_BK)
        ln(cv,EL-2,EY-3,EL+2,EY-1,o);ln(cv,ER-2,EY-1,ER+2,EY-3,o)
    elif style=="sleepy":
        for ex in(EL,ER):
            for dy in range(-1,1):
                for dx in range(-2,3):
                    if dx*dx+dy*dy<=4: px(cv,ex+dx,EY+dy,EY_BK)
            ln(cv,ex-2,EY,ex+2,EY,o)
    elif style=="laser":
        for ex in(EL,ER): ell(cv,ex,EY,2,2,EY_RD)
        ln(cv,0,EY,EL-2,EY,(255,50,50,200));ln(cv,ER+2,EY,W-1,EY,(255,50,50,200))
    elif style=="dead":
        for ex in(EL,ER):
            px(cv,ex-1,EY-1,EY_BK);px(cv,ex+1,EY-1,EY_BK);px(cv,ex,EY,EY_BK)
            px(cv,ex-1,EY+1,EY_BK);px(cv,ex+1,EY+1,EY_BK)
    else:
        for ex in(EL,ER): ell(cv,ex,EY,2,2,EY_BK)

def draw_mouth(cv,style,o):
    MX,MY=12,18;TNK=(218,80,108,255);TPK=(240,140,160,255)
    if style=="smile":
        ln(cv,MX-2,MY-1,MX-1,MY,o);ln(cv,MX-1,MY,MX+1,MY,o);ln(cv,MX+1,MY,MX+2,MY-1,o)
    elif style=="grin":
        ln(cv,MX-3,MY-1,MX-2,MY,o);ln(cv,MX-2,MY,MX+2,MY,o);ln(cv,MX+2,MY,MX+3,MY-1,o)
        rct(cv,MX-1,MY-1,MX+2,MY,(245,243,248,255))
    elif style=="neutral": ln(cv,MX-2,MY,MX+2,MY,o)
    elif style=="tongue":
        ln(cv,MX-2,MY-1,MX+2,MY-1,o);ln(cv,MX-2,MY-1,MX-2,MY+1,o);ln(cv,MX+2,MY-1,MX+2,MY+1,o)
        ell(cv,MX,MY+1,2,2,TNK)
    elif style=="kiss": ell(cv,MX,MY,2,2,TNK);ell(cv,MX,MY,1,1,TPK)
    elif style=="sad":
        ln(cv,MX-2,MY,MX-1,MY+1,o);ln(cv,MX-1,MY+1,MX+1,MY+1,o);ln(cv,MX+1,MY+1,MX+2,MY,o)
    elif style=="open": ell(cv,MX,MY,3,2,o);ell(cv,MX,MY,2,1,(240,60,80,255))
    else: ln(cv,MX-2,MY-1,MX-1,MY,o);ln(cv,MX-1,MY,MX+1,MY,o);ln(cv,MX+1,MY,MX+2,MY-1,o)

def draw_cheeks(cv,style):
    BLU=(248,165,190,80);FRK=(30,22,32,200);STR=(250,210,30,200)
    if style=="blush": ell(cv,4,13,3,2,BLU);ell(cv,20,13,3,2,BLU)
    elif style=="freckles":
        for bx in(3,5,6): px(cv,bx,13,FRK)
        for bx in(18,19,21): px(cv,bx,13,FRK)
    elif style=="stars":
        for sx,sy in[(3,12),(5,14),(4,13)]: px(cv,sx,sy,STR)
        for sx,sy in[(19,12),(21,14),(20,13)]: px(cv,sx,sy,STR)

def draw_neck(cv,style):
    DMN=(180,220,255,255)
    if style=="gold_chain":
        for x in range(6,18): px(cv,x,21+x%2,GLD)
    elif style=="diamond_chain":
        for x in range(6,18): px(cv,x,21+x%2,DMN if x%2==0 else GLD)
        px(cv,12,20,DMN);px(cv,12,23,DMN)
    elif style=="bow_pink":
        ell(cv,8,22,3,2,(220,55,95,255));ell(cv,16,22,3,2,(220,55,95,255));ell(cv,12,22,2,2,(245,168,195,255))
    elif style=="bow_red":
        ell(cv,8,22,3,2,(205,35,55,255));ell(cv,16,22,3,2,(205,35,55,255));ell(cv,12,22,2,2,(245,120,120,255))
    elif style=="scarf_blue":
        rct(cv,4,20,20,23,(55,95,188,255));rct(cv,8,23,11,H,(55,95,188,255))
    elif style=="necktie":
        rct(cv,10,20,14,22,(188,38,55,255));tri(cv,(10,22),(14,22),(12,H),(188,38,55,255))
    elif style=="pearl_necklace":
        for x in range(6,18,2): ell(cv,x,21,1,1,(245,242,240,255))

def draw_eyegear(cv,style,o):
    FR=(22,18,28,255)
    def fp(col):
        for ex in(EL,ER): ell(cv,ex,EY,3,2,FR);ell(cv,ex,EY,2,1,col)
        ln(cv,EL+3,EY,ER-3,EY,FR);ln(cv,0,EY,EL-3,EY,FR);ln(cv,ER+3,EY,W-1,EY,FR)
    if   style=="shades_black": fp((25,22,30,220))
    elif style=="shades_red":   fp((210,35,55,220))
    elif style=="shades_blue":  fp((40,100,220,200))
    elif style=="shades_gold":
        for ex in(EL,ER): ell(cv,ex,EY,3,2,GLD);ell(cv,ex,EY,2,1,(30,20,10,200))
        ln(cv,EL+3,EY,ER-3,EY,GLD);ln(cv,0,EY,EL-3,EY,GLD);ln(cv,ER+3,EY,W-1,EY,GLD)
    elif style=="round_glasses":
        WH2=(245,243,248,200)
        for ex in(EL,ER): ell(cv,ex,EY,3,3,FR);ell(cv,ex,EY,2,2,WH2)
        ln(cv,EL+3,EY,ER-3,EY,FR);ln(cv,0,EY,EL-3,EY,FR);ln(cv,ER+3,EY,W-1,EY,FR)
    elif style=="heart_glasses":
        for ex in(EL,ER): heart_shape(cv,ex,EY,2,(220,40,80,220))
        ln(cv,EL+3,EY,ER-3,EY,FR)
    elif style=="3d_glasses":
        ell(cv,EL,EY,3,2,FR);ell(cv,EL,EY,2,1,(200,30,30,180))
        ell(cv,ER,EY,3,2,FR);ell(cv,ER,EY,2,1,(30,50,200,180))
        ln(cv,EL+3,EY,ER-3,EY,FR);ln(cv,0,EY,EL-3,EY,FR);ln(cv,ER+3,EY,W-1,EY,FR)
    elif style=="vr_headset":
        rct(cv,EL-3,EY-2,ER+4,EY+3,FR)
        rct(cv,EL-2,EY-1,EL+2,EY+2,(30,100,200,220));rct(cv,ER-2,EY-1,ER+3,EY+2,(30,100,200,220))
        ln(cv,0,EY,EL-3,EY,FR);ln(cv,ER+3,EY,W-1,EY,FR)
    elif style=="monocle":
        ell(cv,ER,EY,3,3,(180,140,50,255));ell(cv,ER,EY,2,2,(245,243,248,160))
        ln(cv,ER+2,EY+2,ER+3,EY+5,(180,140,50,255))

def draw_hat(cv,style,o):
    YW2=(250,214,72,255);PK2=(235,110,155,255);BK2=(22,18,28,255)
    if style=="none": return
    elif style=="beanie_red":    ell(cv,12,5,10,6,(205,40,55,255));rct(cv,2,8,22,11,(185,30,45,255))
    elif style=="beanie_blue":   ell(cv,12,5,10,6,(40,95,200,255));rct(cv,2,8,22,11,(30,75,175,255))
    elif style=="beanie_yellow": ell(cv,12,5,10,6,(245,200,15,255));rct(cv,2,8,22,11,(218,175,10,255))
    elif style=="beanie_pink":   ell(cv,12,5,10,6,PK2);rct(cv,2,8,22,11,(210,85,130,255))
    elif style=="cap_black":     ell(cv,12,6,10,7,BK2);rct(cv,2,9,22,12,BK2);rct(cv,18,10,23,13,BK2)
    elif style=="cap_red":       ell(cv,12,6,10,7,(205,38,55,255));rct(cv,2,9,22,12,(185,28,45,255));rct(cv,18,10,23,13,(185,28,45,255))
    elif style=="cap_blue":      ell(cv,12,6,10,7,(38,92,200,255));rct(cv,2,9,22,12,(28,72,175,255));rct(cv,18,10,23,13,(28,72,175,255))
    elif style=="crown_gold":
        rct(cv,5,6,19,10,YW2)
        for cx in[6,9,12,15,18]: rct(cv,cx-1,3,cx+2,7,YW2)
        px(cv,6,4,(220,40,55,255));px(cv,12,3,(100,190,255,255));px(cv,18,4,(220,40,55,255))
    elif style=="crown_pink":
        rct(cv,5,6,19,10,PK2)
        for cx in[6,9,12,15,18]: rct(cv,cx-1,3,cx+2,7,PK2)
        px(cv,6,4,YW2);px(cv,12,3,(255,100,180,255));px(cv,18,4,YW2)
    elif style=="party_hat_pink": tri(cv,(5,9),(19,9),(12,0),PK2);ln(cv,7,7,17,7,(248,168,200,255));ln(cv,9,4,15,4,(248,168,200,255));ell(cv,12,0,2,2,(248,248,248,255))
    elif style=="party_hat_blue": tri(cv,(5,9),(19,9),(12,0),(42,98,210,255));ln(cv,7,7,17,7,(120,168,248,255));ln(cv,9,4,15,4,(120,168,248,255));ell(cv,12,0,2,2,(248,248,248,255))
    elif style=="top_hat":  rct(cv,3,8,21,11,BK2);rct(cv,6,1,18,9,BK2);rct(cv,6,7,18,9,(188,148,38,255))
    elif style=="cowboy":   ell(cv,12,6,10,5,(140,90,40,255));rct(cv,0,8,24,11,(140,90,40,255));rct(cv,6,7,18,9,(115,70,28,255))
    elif style=="flower_pink":
        for dx,dy in[(0,-3),(2,-2),(3,0),(2,2),(0,3),(-2,2),(-3,0),(-2,-2)]: ell(cv,12+dx,4+dy,2,2,(245,142,178,255))
        ell(cv,12,4,2,2,YW2)
    elif style=="flower_white":
        for dx,dy in[(0,-3),(2,-2),(3,0),(2,2),(0,3),(-2,2),(-3,0),(-2,-2)]: ell(cv,12+dx,4+dy,2,2,(248,245,252,255))
        ell(cv,12,4,2,2,YW2)
    elif style=="halo":         ell(cv,12,2,7,2,YW2);ell(cv,12,2,5,1,(255,245,200,80))
    elif style=="devil_horns":  tri(cv,(5,8),(9,8),(6,2),(185,28,28,255));tri(cv,(15,8),(19,8),(18,2),(185,28,28,255))
    elif style=="bucket_hat":   ell(cv,12,7,10,5,(145,145,155,255));rct(cv,2,10,22,13,(120,120,130,255))
    elif style=="snapback":     ell(cv,12,6,10,7,BK2);rct(cv,2,9,22,11,BK2);rct(cv,18,10,23,12,BK2);rct(cv,7,8,17,9,(55,52,62,255))

def draw_bear(bg_key, fur_key, eye, mouth, cheek, hat, eyegear, neck):
    fur=FUR[fur_key]; o,m,l,f=fur['o'],fur['m'],fur['l'],fur['f']
    cv=make_cv(BACKGROUNDS[bg_key])
    ell(cv,12,26,10,8,o);ell(cv,12,26,9,7,m)
    ell(cv,12,12,11,11,o);ell(cv,12,12,10,10,m)
    for ex in(6,18): ell(cv,ex,4,4,4,o);ell(cv,ex,4,3,3,m);ell(cv,ex,4,2,2,l)
    ell(cv,12,16,6,5,l);ell(cv,12,16,5,4,f)
    draw_eyes(cv,eye,o); ell(cv,12,15,2,1,o);px(cv,12,14,(255,255,255,150))
    draw_mouth(cv,mouth,o);draw_cheeks(cv,cheek)
    draw_neck(cv,neck);draw_eyegear(cv,eyegear,o);draw_hat(cv,hat,o)
    return cv

# ══════════════════════════════════════════════════════════════════
#  Trait reverse-map  (metadata label → internal key)
# ══════════════════════════════════════════════════════════════════
BG_REV   = {"Vivid Orange":"vivid_orange","Cobalt Blue":"cobalt_blue","Royal Purple":"royal_purple","Emerald":"emerald","Crimson":"crimson","Golden":"golden","Hot Pink":"hot_pink","Teal":"teal","Midnight":"midnight","Electric Lime":"electric_lime","Sky Blue":"sky_blue","Warm White":"warm_white"}
FUR_REV  = {"Honey Brown":"honey_brown","Midnight Black":"midnight_blk","Polar White":"polar_white","Golden Bear":"golden_bear","Steel Gray":"steel_gray","Rose Pink":"rose_pink","Sky Blue":"sky_blue_fur","Lavender":"lavender_fur","Zombie Green":"zombie_grn","Alien Teal":"alien_teal"}
EYE_REV  = {"Normal":"normal","Closed":"closed","Wink (Right)":"wink_r","Wink (Left)":"wink_l","Heart":"heart","Star":"star","Angry":"angry","Sleepy":"sleepy","Laser":"laser","Dead":"dead"}
MTH_REV  = {"Smile":"smile","Grin":"grin","Neutral":"neutral","Tongue":"tongue","Kiss":"kiss","Sad":"sad","Open":"open"}
CHK_REV  = {"Blush":"blush","None":"none","Freckles":"freckles","Stars":"stars"}
HAT_REV  = {"None":"none","Red Beanie":"beanie_red","Blue Beanie":"beanie_blue","Yellow Beanie":"beanie_yellow","Pink Beanie":"beanie_pink","Black Cap":"cap_black","Red Cap":"cap_red","Blue Cap":"cap_blue","Pink Party Hat":"party_hat_pink","Blue Party Hat":"party_hat_blue","Bucket Hat":"bucket_hat","Snapback":"snapback","Pink Flower":"flower_pink","White Flower":"flower_white","Cowboy Hat":"cowboy","Top Hat":"top_hat","Gold Crown":"crown_gold","Pink Crown":"crown_pink","Halo":"halo","Devil Horns":"devil_horns"}
EWR_REV  = {"None":"none","Black Shades":"shades_black","Red Shades":"shades_red","Blue Shades":"shades_blue","Gold Shades":"shades_gold","Round Glasses":"round_glasses","Heart Glasses":"heart_glasses","3D Glasses":"3d_glasses","Vr Headset":"vr_headset","Monocle":"monocle"}
NCK_REV  = {"None":"none","Gold Chain":"gold_chain","Diamond Chain":"diamond_chain","Pink Bow":"bow_pink","Red Bow":"bow_red","Blue Scarf":"scarf_blue","Necktie":"necktie","Pearl Necklace":"pearl_necklace"}

def parse_meta(raw):
    """Extract internal trait keys from a metadata dict."""
    attrs = {a['trait_type']: a['value'] for a in raw['attributes']}
    return {
        'bg_orig': BG_REV.get(attrs.get('Background',''),'vivid_orange'),
        'fur':     FUR_REV.get(attrs.get('Fur',''),'honey_brown'),
        'eye':     EYE_REV.get(attrs.get('Eyes',''),'normal'),
        'mouth':   MTH_REV.get(attrs.get('Mouth',''),'smile'),
        'cheek':   CHK_REV.get(attrs.get('Cheeks',''),'blush'),
        'hat':     HAT_REV.get(attrs.get('Hat',''),'none'),
        'eyegear': EWR_REV.get(attrs.get('Eyewear',''),'none'),
        'neck':    NCK_REV.get(attrs.get('Neck',''),'none'),
        'rarity':  attrs.get('Rarity','Common'),
        'name':    raw.get('name','Baby Bear'),
    }

# ══════════════════════════════════════════════════════════════════
#  Metadata Loader
# ══════════════════════════════════════════════════════════════════
BEARS = {}   # {token_id(int): parsed_traits_dict}

def load_metadata():
    # Try single all_metadata.json first (deployment-friendly)
    if os.path.exists(ALL_META_FILE):
        print(f"  Loading all_metadata.json…", end='', flush=True)
        t0 = time.time()
        with open(ALL_META_FILE) as f:
            all_meta = json.load(f)
        for item in all_meta:
            name = item.get('name','')
            try:
                tid = int(name.split('#')[1])
            except Exception:
                continue
            BEARS[tid] = parse_meta(item)
        print(f" {len(BEARS):,} bears loaded ({time.time()-t0:.1f}s)")
        return
    # Fallback: individual files
    if not os.path.isdir(META_DIR):
        print(f"  ⚠️  Metadata not found. Need all_metadata.json or metadata/ folder.")
        return
    print(f"  Loading metadata…", end='', flush=True)
    t0 = time.time()
    for i in range(1, 10001):
        p = os.path.join(META_DIR, f"{i}.json")
        if os.path.exists(p):
            with open(p) as f:
                BEARS[i] = parse_meta(json.load(f))
    print(f" {len(BEARS):,} bears loaded ({time.time()-t0:.1f}s)")

# ══════════════════════════════════════════════════════════════════
#  BTC Sentiment (20-minute change via Binance kline)
# ══════════════════════════════════════════════════════════════════
_sent_cache = {'pct': 0.0, 'price_now': 0, 'price_20m': 0, 'key': 'up_low',
               'bg_key': 'sent_purple', 'emoji': '💜', 'label': 'SLIGHT UP',
               'css': '#7b3db8', 'ts': 0, 'source': 'init'}
_sent_lock  = threading.Lock()

def fetch_sentiment():
    """Fetch 20-minute BTC change. Tries 3 sources in order."""
    hdrs = {'User-Agent': 'Mozilla/5.0'}

    # ── 1. CryptoCompare 1-min OHLCV (most accurate) ─────────────
    try:
        url = 'https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USD&limit=20'
        with urlopen(Request(url, headers=hdrs), timeout=10, context=_ssl_ctx) as r:
            data = json.loads(r.read())
        candles   = data['Data']['Data']
        price_20m = float(candles[0]['open'])   # price 20 min ago
        price_now = float(candles[-1]['close'])  # current price
        pct = (price_now - price_20m) / price_20m * 100
        return pct, price_now, price_20m, 'CryptoCompare'
    except Exception as e:
        print(f'  CryptoCompare fail: {e}')

    # ── 2. Kraken 1-min OHLC ─────────────────────────────────────
    try:
        since = int(time.time()) - 22 * 60
        url = f'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1&since={since}'
        with urlopen(Request(url, headers=hdrs), timeout=10, context=_ssl_ctx) as r:
            data = json.loads(r.read())
        candles   = data['result']['XXBTZUSD']
        price_20m = float(candles[0][1])    # open of oldest 1-min candle ≈ 20m ago
        price_now = float(candles[-1][4])   # close of newest candle
        pct = (price_now - price_20m) / price_20m * 100
        return pct, price_now, price_20m, 'Kraken'
    except Exception as e:
        print(f'  Kraken fail: {e}')

    # ── 3. CoinGecko simple price (current only, fallback) ────────
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true'
        with urlopen(Request(url, headers=hdrs), timeout=12, context=_ssl_ctx) as r:
            data = json.loads(r.read())
        price_now = float(data['bitcoin']['usd'])
        pct_24h   = float(data['bitcoin'].get('usd_24h_change', 0))
        # estimate 20m from 24h rate (rough fallback)
        pct = pct_24h / 72
        price_20m = price_now / (1 + pct / 100)
        return pct, price_now, price_20m, 'CoinGecko'
    except Exception as e:
        print(f'  CoinGecko fail: {e}')

    return None, None, None, 'error'

def refresh_sentiment():
    pct, price_now, price_20m, source = fetch_sentiment()
    if pct is None:
        return False
    key, bg_key, emoji, label, css = pct_to_sentiment(pct)
    with _sent_lock:
        _sent_cache.update(dict(pct=pct, price_now=price_now, price_20m=price_20m,
                                key=key, bg_key=bg_key, emoji=emoji, label=label,
                                css=css, ts=time.time(), source=source))
    print(f"  💰  BTC: ${price_now:>10,.0f}  (20m ago ${price_20m:,.0f})  "
          f"Δ {pct:+.2f}%  → {emoji} {label}  [{source}]")
    return True

def get_sentiment():
    with _sent_lock:
        now = time.time()
        is_init = _sent_cache['source'] == 'init'
        is_stale = now - _sent_cache['ts'] > PRICE_TTL

    if is_init:
        # Cold start — block until we have real data (max 15s)
        refresh_sentiment()
    elif is_stale:
        threading.Thread(target=refresh_sentiment, daemon=True).start()

    with _sent_lock:
        return dict(_sent_cache)

# ══════════════════════════════════════════════════════════════════
#  Image Cache  (keyed by token_id + sentiment bg_key)
# ══════════════════════════════════════════════════════════════════
_img_cache     = {}
_cache_sent_key = None
_cache_lock2   = threading.Lock()

def get_bear_image(token_id):
    sent = get_sentiment()
    bg_key = sent['bg_key']

    with _cache_lock2:
        global _cache_sent_key
        if bg_key != _cache_sent_key:
            _img_cache.clear()
            _cache_sent_key = bg_key
        if token_id in _img_cache:
            return _img_cache[token_id], sent

    traits = BEARS.get(token_id)
    if not traits:
        return None, sent

    cv  = draw_bear(bg_key, traits['fur'], traits['eye'], traits['mouth'],
                    traits['cheek'], traits['hat'], traits['eyegear'], traits['neck'])
    png = bear_to_png(cv)

    with _cache_lock2:
        # Cache the whole collection per sentiment (~5KB/bear → ~50MB at 10k).
        # Cleared automatically when the sentiment background changes.
        if len(_img_cache) < 10000:
            _img_cache[token_id] = png
    return png, sent

# ══════════════════════════════════════════════════════════════════
#  Mint Page
# ══════════════════════════════════════════════════════════════════
CONTRACT_ADDRESS = "0xAF6a5e744Ff06d50c2F236b90344F84A640381A9"
MINT_PRICE_MATIC = "0.003"

def build_mint_html():
    base = BASE_URL if BASE_URL else "http://localhost:8889"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🐻 Baby Bear NFT — Mint</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a12;color:#e0e0e0;font-family:'Courier New',monospace;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px}}
h1{{font-size:1.6rem;color:#fff;margin-bottom:4px;letter-spacing:2px}}
.sub{{color:#666;font-size:.8rem;margin-bottom:32px}}
.card{{background:#13131f;border:1px solid #2a2a3a;border-radius:12px;padding:32px;width:100%;max-width:440px;display:flex;flex-direction:column;gap:20px}}
.bear-preview{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:4px}}
.bear-preview img{{width:100%;border-radius:4px;image-rendering:pixelated}}
.info-row{{display:flex;justify-content:space-between;font-size:.85rem;color:#888;border-bottom:1px solid #1e1e2e;padding-bottom:12px}}
.info-row span{{color:#ccc}}
.price-box{{background:#0d0d1a;border:1px solid #2a2a3a;border-radius:8px;padding:14px;text-align:center}}
.price-big{{font-size:1.5rem;color:#fff;font-weight:bold}}
.price-usd{{font-size:.75rem;color:#555;margin-top:2px}}
.qty-row{{display:flex;align-items:center;gap:12px;justify-content:center}}
.qty-btn{{background:#1e1e2e;border:1px solid #333;color:#fff;width:36px;height:36px;border-radius:6px;font-size:1.2rem;cursor:pointer}}
.qty-btn:hover{{background:#2a2a3a}}
#qty{{background:#0d0d1a;border:1px solid #333;color:#fff;width:60px;height:36px;text-align:center;border-radius:6px;font-size:1rem}}
.total-cost{{text-align:center;font-size:.85rem;color:#666}}
.total-cost span{{color:#aaa}}
button#mintBtn{{width:100%;padding:14px;border:none;border-radius:8px;font-size:1rem;font-family:'Courier New',monospace;font-weight:bold;cursor:pointer;letter-spacing:1px;transition:all .2s}}
#mintBtn.connect{{background:linear-gradient(135deg,#7b3db8,#4a1d8a);color:#fff}}
#mintBtn.connect:hover{{background:linear-gradient(135deg,#9b5dd8,#6a3daa)}}
#mintBtn.mint{{background:linear-gradient(135deg,#d63087,#8b1a5a);color:#fff}}
#mintBtn.mint:hover{{background:linear-gradient(135deg,#e650a7,#ab3a7a)}}
#mintBtn:disabled{{background:#2a2a3a;color:#555;cursor:not-allowed}}
.status{{text-align:center;font-size:.8rem;min-height:20px;padding:4px}}
.status.ok{{color:#4caf50}}
.status.err{{color:#f44336}}
.status.pending{{color:#ff9800}}
.progress{{background:#1e1e2e;border-radius:4px;height:6px;overflow:hidden}}
.progress-bar{{height:100%;background:linear-gradient(90deg,#7b3db8,#d63087);transition:width .5s}}
.supply-text{{display:flex;justify-content:space-between;font-size:.75rem;color:#555;margin-top:4px}}
a.back{{color:#555;font-size:.75rem;text-decoration:none;margin-top:16px}}
a.back:hover{{color:#888}}
</style>
</head>
<body>
<h1>🐻 Baby Bear NFT</h1>
<p class="sub">Living NFT — background changes with BTC sentiment</p>

<div class="card">
  <div class="bear-preview">
    <img src="{base}/bears/1/image.png">
    <img src="{base}/bears/500/image.png">
    <img src="{base}/bears/1000/image.png">
    <img src="{base}/bears/2000/image.png">
  </div>

  <div class="info-row">
    <div>Contract</div>
    <span style="font-size:.7rem">{CONTRACT_ADDRESS[:10]}...{CONTRACT_ADDRESS[-6:]}</span>
  </div>
  <div class="info-row">
    <div>Network</div><span>Polygon</span>
  </div>
  <div class="info-row">
    <div>Supply</div><span>10,000</span>
  </div>

  <div class="price-box">
    <div class="price-big">{MINT_PRICE_MATIC} POL</div>
    <div class="price-usd">per bear · ~$0.001</div>
  </div>

  <div>
    <div class="qty-row">
      <button class="qty-btn" onclick="changeQty(-1)">−</button>
      <input id="qty" type="number" value="1" min="1" max="20" oninput="updateCost()">
      <button class="qty-btn" onclick="changeQty(1)">+</button>
    </div>
    <div class="total-cost" style="margin-top:8px">Total: <span id="totalCost">{MINT_PRICE_MATIC} POL</span></div>
  </div>

  <div>
    <div class="progress"><div class="progress-bar" id="progBar" style="width:0%"></div></div>
    <div class="supply-text"><span id="minted">— minted</span><span>10,000 total</span></div>
  </div>

  <button id="mintBtn" class="connect" onclick="handleClick()">Connect Wallet</button>
  <div class="status" id="status"></div>
</div>
<a class="back" href="{base}">← Back to dashboard</a>

<script>
const CONTRACT = "{CONTRACT_ADDRESS}";
const PRICE    = BigInt("{int(float(MINT_PRICE_MATIC)*1e18)}");
const ABI = [
  {{"inputs":[{{"name":"qty","type":"uint256"}}],"name":"mint","outputs":[],"stateMutability":"payable","type":"function"}},
  {{"inputs":[],"name":"mintPrice","outputs":[{{"name":"","type":"uint256"}}],"stateMutability":"view","type":"function"}},
  {{"inputs":[],"name":"totalMinted","outputs":[{{"name":"","type":"uint256"}}],"stateMutability":"view","type":"function"}},
  {{"inputs":[],"name":"MAX_SUPPLY","outputs":[{{"name":"","type":"uint256"}}],"stateMutability":"view","type":"function"}},
  {{"inputs":[],"name":"mintOpen","outputs":[{{"name":"","type":"bool"}}],"stateMutability":"view","type":"function"}}
];

let provider, signer, contract, connected = false;

function changeQty(d) {{
  const el = document.getElementById('qty');
  el.value = Math.max(1, Math.min(20, parseInt(el.value||1) + d));
  updateCost();
}}
function updateCost() {{
  const q = parseInt(document.getElementById('qty').value)||1;
  const total = (parseFloat("{MINT_PRICE_MATIC}") * q).toFixed(4);
  document.getElementById('totalCost').textContent = total + ' POL';
}}
function setStatus(msg, cls='') {{
  const s = document.getElementById('status');
  s.textContent = msg; s.className = 'status ' + cls;
}}

async function loadSupply() {{
  try {{
    const p = new ethers.JsonRpcProvider('https://polygon-rpc.com');
    const c = new ethers.Contract(CONTRACT, ABI, p);
    const [minted, open] = await Promise.all([c.totalMinted(), c.mintOpen()]);
    const m = Number(minted);
    document.getElementById('minted').textContent = m.toLocaleString() + ' minted';
    document.getElementById('progBar').style.width = (m/10000*100) + '%';
    if (!open) setStatus('Mint not open yet', 'err');
  }} catch(e) {{ console.log(e); }}
}}

async function handleClick() {{
  if (!window.ethereum) {{ setStatus('MetaMask not detected. Install MetaMask first.', 'err'); return; }}
  if (!connected) {{
    try {{
      provider = new ethers.BrowserProvider(window.ethereum);
      await provider.send('eth_requestAccounts', []);
      const net = await provider.getNetwork();
      if (net.chainId !== 137n) {{
        setStatus('Switch MetaMask to Polygon network', 'err');
        try {{ await window.ethereum.request({{method:'wallet_switchEthereumChain',params:[{{chainId:'0x89'}}]}}); }} catch(e) {{}}
        return;
      }}
      signer = await provider.getSigner();
      contract = new ethers.Contract(CONTRACT, ABI, signer);
      connected = true;
      const btn = document.getElementById('mintBtn');
      btn.textContent = 'Mint 🐻';
      btn.className = 'mint';
      setStatus('Wallet connected: ' + (await signer.getAddress()).slice(0,6) + '...', 'ok');
    }} catch(e) {{ setStatus('Connection failed: ' + e.message, 'err'); }}
    return;
  }}
  // Mint
  const qty = parseInt(document.getElementById('qty').value)||1;
  const btn = document.getElementById('mintBtn');
  btn.disabled = true;
  setStatus('Confirm in MetaMask...', 'pending');
  try {{
    const value = PRICE * BigInt(qty);
    const tx = await contract.mint(qty, {{value}});
    setStatus('Transaction sent, waiting...', 'pending');
    await tx.wait();
    setStatus('🎉 Minted ' + qty + ' bear(s)! Tx: ' + tx.hash.slice(0,10) + '...', 'ok');
    loadSupply();
  }} catch(e) {{
    setStatus('Failed: ' + (e.reason || e.message || 'Unknown error'), 'err');
  }}
  btn.disabled = false;
}}

// Load ethers.js then supply
const s = document.createElement('script');
s.src = 'https://cdnjs.cloudflare.com/ajax/libs/ethers/6.7.0/ethers.umd.min.js';
s.onload = loadSupply;
document.head.appendChild(s);
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════
#  HTML Dashboard
# ══════════════════════════════════════════════════════════════════
def build_html(sent, total):
    pct       = sent['pct']
    price_now = sent['price_now']
    price_20m  = sent['price_20m']
    emoji     = sent['emoji']
    label     = sent['label']
    css       = sent['css']
    sign      = '+' if pct >= 0 else ''
    age       = int(time.time() - sent['ts'])

    preview_grid = ''.join(
        f'<a href="/bears/{i}/image.png" target="_blank" title="Bear #{i}">'
        f'<img src="/bears/{i}/image.png?t={int(time.time())}" '
        f'class="grid-img" alt="#{i}" loading="lazy"></a>'
        for i in PREVIEW_IDS if i in BEARS
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🐻 Baby Bear NFT — Sentiment Edition</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080810;color:#d8d8e8;font-family:'Courier New',monospace;min-height:100vh;padding:24px}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:1.6rem;letter-spacing:3px;margin-bottom:4px}}
.sub{{color:#333;font-size:.7rem;margin-bottom:24px;letter-spacing:1px}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}}
@media(max-width:600px){{.row{{grid-template-columns:1fr}}}}

/* sentiment card */
.sent-card{{background:#0e0e1e;border:2px solid {css};border-radius:14px;padding:20px;position:relative;overflow:hidden}}
.sent-card::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at 50% 0%,{css}22 0%,transparent 70%)}}
.sent-emoji{{font-size:2.8rem;line-height:1}}
.sent-label{{font-size:1.1rem;font-weight:bold;color:{css};letter-spacing:2px;margin:6px 0 2px}}
.sent-pct{{font-size:2rem;font-weight:bold}}
.sent-sub{{color:#444;font-size:.65rem;margin-top:6px}}

/* price card */
.price-card{{background:#0e0e1e;border:1px solid #1e1e32;border-radius:14px;padding:20px}}
.price-now{{font-size:2rem;font-weight:bold;margin:8px 0}}
.price-8h{{color:#444;font-size:.8rem}}
.price-chg{{font-size:.9rem;color:{css};font-weight:bold}}
.p-label{{color:#333;font-size:.62rem;text-transform:uppercase;letter-spacing:2px}}

/* threshold bar */
.bar-wrap{{background:#0e0e1e;border:1px solid #1e1e32;border-radius:14px;padding:20px;margin-bottom:20px}}
.bar-title{{color:#333;font-size:.62rem;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px}}
.bar-track{{background:#111;border-radius:8px;height:24px;position:relative;overflow:hidden;border:1px solid #1e1e32}}
/* segments aligned to the -2%..+3% cursor scale: boundaries at -1/0/+1/+2 → 20% each */
.seg-black {{position:absolute;left:0;top:0;width:20%;height:100%;background:#111118}}
.seg-blue  {{position:absolute;left:20%;top:0;width:20%;height:100%;background:#1565c0}}
.seg-purple{{position:absolute;left:40%;top:0;width:20%;height:100%;background:#7b3db8}}
.seg-yellow{{position:absolute;left:60%;top:0;width:20%;height:100%;background:#d4a800}}
.seg-pink  {{position:absolute;left:80%;top:0;width:20%;height:100%;background:#d63087}}
.bar-cursor{{position:absolute;top:0;width:4px;height:100%;background:#fff;border-radius:2px;box-shadow:0 0 8px #fff;transition:left .6s ease}}
.bar-ticks{{display:flex;justify-content:space-between;margin-top:6px;font-size:.58rem;color:#333}}

/* grid */
.grid-title{{color:#333;font-size:.62rem;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px;margin-bottom:20px}}
.grid-img{{width:100%;aspect-ratio:1;border-radius:6px;image-rendering:pixelated;image-rendering:crisp-edges;border:1px solid #1e1e32;transition:transform .15s;display:block}}
.grid-img:hover{{transform:scale(1.06);border-color:{css}}}

/* search */
.search-box{{background:#0e0e1e;border:1px solid #1e1e32;border-radius:14px;padding:20px;margin-bottom:20px}}
.search-row{{display:flex;gap:10px;margin-top:10px}}
.inp{{background:#111;border:1px solid #222;color:#d8d8e8;border-radius:8px;padding:8px 12px;font-family:inherit;font-size:.85rem;flex:1}}
.btn{{background:{css};color:#fff;border:none;border-radius:8px;padding:8px 18px;cursor:pointer;font-family:inherit;font-size:.85rem;font-weight:bold}}
.bear-result{{margin-top:16px;display:none;text-align:center}}
.bear-result img{{width:200px;height:200px;image-rendering:pixelated;border-radius:10px;border:2px solid {css}}}

/* legend */
.legend{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}}
.leg-item{{display:flex;align-items:center;gap:6px;font-size:.65rem;color:#888}}
.leg-dot{{width:12px;height:12px;border-radius:3px}}

/* footer */
.footer{{color:#222;font-size:.6rem;margin-top:10px;text-align:center}}
.info-row{{display:flex;justify-content:space-between;font-size:.62rem;color:#2a2a4a;margin-top:8px}}
.mint-link{{display:inline-block;margin-bottom:24px;padding:10px 28px;background:linear-gradient(135deg,#d63087,#7b3db8);color:#fff;text-decoration:none;border-radius:8px;font-size:.85rem;font-weight:bold;letter-spacing:1px}}
.mint-link:hover{{opacity:.85}}
</style>
</head><body><div class="wrap">
<h1>🐻 Baby Bear NFT</h1>
<p class="sub">SENTIMENT EDITION — {total:,} BEARS · BACKGROUND DRIVEN BY 20M BTC CHANGE</p>
<a class="mint-link" href="/mint">🐻 MINT NOW — 0.003 POL</a>

<div class="row">
  <div class="sent-card">
    <div class="sent-emoji">{emoji}</div>
    <div class="sent-label">{label}</div>
    <div class="sent-pct">{sign}{pct:.2f}%</div>
    <div class="sent-sub">20-minute change · source: {sent['source']} · {age}s ago</div>
  </div>
  <div class="price-card">
    <div class="p-label">Bitcoin Price</div>
    <div class="price-now">${price_now:>,.0f}</div>
    <div class="price-8h">20m ago: ${price_20m:>,.0f}</div>
    <div class="price-chg">{sign}{pct:.2f}% in last 20 minutes</div>
  </div>
</div>

<div class="bar-wrap">
  <div class="bar-title">Sentiment Threshold Visualiser</div>
  <div class="bar-track">
    <div class="seg-black"></div><div class="seg-blue"></div>
    <div class="seg-purple"></div><div class="seg-yellow"></div><div class="seg-pink"></div>
    <div class="bar-cursor" id="cur"></div>
  </div>
  <div class="bar-ticks"><span>−∞</span><span>−1%🖤</span><span>0%💜</span><span>+1%🟡</span><span>+2%🌸</span><span>+∞</span></div>
</div>

<div class="legend">
  <div class="leg-item"><div class="leg-dot" style="background:#111118"></div> 🖤 DN ≥1% — Black</div>
  <div class="leg-item"><div class="leg-dot" style="background:#1565c0"></div> 💙 DN &lt;1% — Blue</div>
  <div class="leg-item"><div class="leg-dot" style="background:#7b3db8"></div> 💜 UP &lt;1% — Purple</div>
  <div class="leg-item"><div class="leg-dot" style="background:#d4a800"></div> 🟡 UP 1–2% — Yellow</div>
  <div class="leg-item"><div class="leg-dot" style="background:#d63087"></div> 🌸 UP ≥2% — Pink</div>
</div>

<div class="grid-title">Collection Preview (20 Bears · Sentiment Background Active)</div>
<div class="grid">{preview_grid}</div>

<div class="search-box">
  <div class="grid-title">Browse Individual Bear</div>
  <div class="search-row">
    <input class="inp" id="tid" type="number" min="1" max="10000" placeholder="Token ID  (1 – {total:,})">
    <button class="btn" onclick="loadBear()">View Bear</button>
  </div>
  <div class="bear-result" id="res">
    <img id="res-img" src="" alt="">
    <p id="res-name" style="color:#888;font-size:.75rem;margin-top:8px"></p>
  </div>
</div>

<div class="info-row">
  <span>🔄 Auto-refresh every {PRICE_TTL}s</span>
  <span id="cd">{PRICE_TTL}s</span>
  <span>{BASE_URL if BASE_URL else f'localhost:{PORT}'}</span>
</div>
<div class="footer">Baby Bear NFT — Sentiment Edition · Each bear's background updates with the market</div>
</div>

<script>
// Position the cursor on the sentiment bar
const pct={pct:.4f};
// Map pct to bar position: range -3% to +4%
const minP=-2, maxP=3;
const frac=Math.min(Math.max((pct-minP)/(maxP-minP),0),1);
document.getElementById('cur').style.left=(frac*100)+'%';

function loadBear(){{
  const id=parseInt(document.getElementById('tid').value)||1;
  const img=document.getElementById('res-img');
  const nm =document.getElementById('res-name');
  img.src='/bears/'+id+'/image.png?t='+Date.now();
  nm.textContent='Baby Bear #'+id+' · Click image to open full size';
  img.onclick=()=>window.open('/bears/'+id+'/image.png','_blank');
  document.getElementById('res').style.display='block';
}}

// Countdown + auto-refresh
let cd={PRICE_TTL};
setInterval(()=>{{
  cd--;
  const el=document.getElementById('cd');
  if(el) el.textContent=cd+'s';
  if(cd<=0) location.reload();
}},1000);
</script>
</body></html>"""

# ══════════════════════════════════════════════════════════════════
#  HTTP Server
# ══════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send(self, code, ctype, body):
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _base_url(self):
        """Public base URL for absolute links in served metadata."""
        if BASE_URL:
            return BASE_URL
        host = self.headers.get('Host', f'localhost:{PORT}')
        return f'http://{host}'

    def do_GET(self):
        path = self.path.split('?')[0]

        # Dashboard
        if path == '/':
            sent = get_sentiment()
            self._send(200, 'text/html; charset=utf-8', build_html(sent, len(BEARS)))

        # Individual bear image
        elif path.startswith('/bears/') and path.endswith('/image.png'):
            try: token_id = int(path.split('/')[2])
            except: self._send(400,'text/plain','bad id'); return
            img, sent = get_bear_image(token_id)
            if img is None: self._send(404,'text/plain','Bear not found'); return
            self._send(200, 'image/png', img)

        # Individual metadata
        elif path.startswith('/bears/') and path.endswith('/metadata.json'):
            try: token_id = int(path.split('/')[2])
            except: self._send(400,'text/plain','bad id'); return
            traits = BEARS.get(token_id)
            sent   = get_sentiment()
            if not traits: self._send(404,'application/json','{"error":"not found"}'); return
            base = self._base_url()
            meta = {
                "name":        traits['name'],
                "description": "Baby Bear NFT — background changes live with 20-minute BTC sentiment.",
                "image":       f"{base}/bears/{token_id}/image.png",
                "external_url": base,
                "attributes":  [
                    {"trait_type":"Sentiment Background","value": f"{sent['emoji']} {sent['label']}"},
                    {"trait_type":"BTC 20m Change",       "value": f"{sent['pct']:+.2f}%"},
                    {"trait_type":"Fur",        "value": traits['fur'].replace('_',' ').title()},
                    {"trait_type":"Eyes",       "value": traits['eye'].title()},
                    {"trait_type":"Hat",        "value": traits['hat'].replace('_',' ').title()},
                    {"trait_type":"Eyewear",    "value": traits['eyegear'].replace('_',' ').title()},
                    {"trait_type":"Neck",       "value": traits['neck'].replace('_',' ').title()},
                    {"trait_type":"Rarity",     "value": traits['rarity']},
                ]
            }
            self._send(200,'application/json',json.dumps(meta,indent=2))

        # API status
        elif path == '/api/sentiment':
            sent = get_sentiment()
            self._send(200,'application/json',json.dumps({
                "pct":       sent['pct'],
                "price_now": sent['price_now'],
                "price_20m":  sent['price_20m'],
                "sentiment": sent['label'],
                "emoji":     sent['emoji'],
                "bg_key":    sent['bg_key'],
                "source":    sent['source'],
                "cache_age": int(time.time()-sent['ts']),
                "total_bears": len(BEARS),
            }))

        elif path.startswith('/simulate/'):
            try:
                pct = float(path.split('/simulate/')[1])
            except (IndexError, ValueError):
                self._send(400,'text/plain','Bad pct'); return
            key, bg_key, emoji, label, css = pct_to_sentiment(pct)
            self._send(200,'application/json',json.dumps({
                "pct":       pct,
                "sentiment": label,
                "emoji":     emoji,
                "bg_key":    bg_key,
                "css_color": css,
            }))

        elif path == '/mint':
            self._send(200, 'text/html; charset=utf-8', build_mint_html())

        elif path == '/banner.png':
            self._send(200, 'image/png', build_banner_png())

        elif path == '/collection.json':
            base = self._base_url()
            self._send(200,'application/json',json.dumps({
                "name":            "Baby Bear",
                "description":     "10,000 pixel baby bears that breathe with Bitcoin. Each bear's background shifts in real time with the 20-minute BTC price change — 🖤 Black (bearish) · 💙 Blue (slight down) · 💜 Purple (slight up) · 🟡 Yellow (bullish) · 🌸 Pink (hot bull). Mint yours and watch it react to the market, 24/7.",
                "image":           f"{base}/bears/1/image.png",
                "banner_image_url": f"{base}/banner.png",
                "external_link":   base,
                "seller_fee_basis_points": 500,
                "fee_recipient":   "0xC4257c62627d8A9945838B7fa5507fda01c38694",
            }))

        else:
            self._send(404,'text/plain','Not found')

# ══════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════
def main():
    print(f"\n🐻  Baby Bear Sentiment Server")
    print(f"    Sentiment colours: 💜 Purple | 🟡 Yellow | 🌸 Pink | 💙 Blue | 🖤 Black\n")

    load_metadata()
    if not BEARS:
        print("  ⚠️  No bears loaded.  Run baby_bear_10k.py first.\n"); return

    print(f"  Fetching initial BTC sentiment…")
    if not refresh_sentiment():
        print(f"  ⚠️  Price fetch failed — using fallback (UP<1% / purple)")

    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"\n🚀  Server ready")
    print(f"    Dashboard   →  http://localhost:{PORT}/")
    print(f"    Bear #1     →  http://localhost:{PORT}/bears/1/image.png")
    print(f"    Metadata #1 →  http://localhost:{PORT}/bears/1/metadata.json")
    print(f"    API         →  http://localhost:{PORT}/api/sentiment")
    print(f"    Price cache : every {PRICE_TTL}s\n")
    print(f"    Press Ctrl+C to stop\n")

    try: server.serve_forever()
    except KeyboardInterrupt: print("\n    Stopped.")

if __name__ == '__main__':
    main()
