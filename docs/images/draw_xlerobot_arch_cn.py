"""Original architecture diagram of the XLeRobot elder-care robot (Chinese edition)."""
import os

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle

# Noto CJK ships as a .ttc whose first face is JP. Prefer the extracted SC OTFs
# (see docs/images/README.md), falling back to the system TTC for Han glyphs.
_HERE = os.path.dirname(os.path.abspath(__file__))
_font_family = None
for _font_dir in ("/tmp/opencode/fonts", os.path.join(_HERE, "fonts")):
    _regular = os.path.join(_font_dir, "NotoSansCJKsc-Regular.otf")
    if os.path.exists(_regular):
        font_manager.fontManager.addfont(_regular)
        _bold = os.path.join(_font_dir, "NotoSansCJKsc-Bold.otf")
        if os.path.exists(_bold):
            font_manager.fontManager.addfont(_bold)
        _font_family = "Noto Sans CJK SC"
        break
if _font_family is None:
    _ttc = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    if os.path.exists(_ttc):
        font_manager.fontManager.addfont(_ttc)
        _font_family = "Noto Sans CJK JP"
plt.rcParams["font.sans-serif"] = [_font_family or "sans-serif"]
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

PAL = {
    "app": dict(front="#A8C8EF", top="#C9DFF7", side="#6F97CF", edge="#2F5B9E"),
    "bus": dict(front="#F7C48C", top="#FBDBB4", side="#E09A52", edge="#C9701C"),
    "svc": dict(front="#BDDDA9", top="#D6EDC8", side="#8FBD78", edge="#4E8A3C"),
    "hal": dict(front="#DCDCDC", top="#F0F0F0", side="#B4B4B4", edge="#7A7A7A"),
    "app_box": dict(front="#5D87C4", top="#93B3DE", side="#3F639C", edge="#27497C"),
    "svc_box": dict(front="#7FB368", top="#A9CE98", side="#5C8C48", edge="#3E6E30"),
    "ros_box": dict(front="#6FA8B8", top="#A4CCD6", side="#4E7E8C", edge="#345E6A"),
    "band_sup": dict(front="#C9B8E0", edge="#6E5396"),
    "band_safe": dict(front="#E8A7A0", edge="#A23F37"),
    "arrow": "#E08A2E",
    "ink": "#1B1B1B",
}

DX, DY = 0.16, 0.10
LX, RX = 0.25, 13.75
TITLE_X = 1.15


def slab(ax, y0, y1, c):
    x0, x1 = LX, RX
    ax.add_patch(Polygon([(x1, y0), (x1, y1), (x1 + DX, y1 + DY), (x1 + DX, y0 + DY)],
                         closed=True, facecolor=c["side"], edgecolor=c["edge"], lw=1.3, zorder=2))
    ax.add_patch(Polygon([(x0, y1), (x1, y1), (x1 + DX, y1 + DY), (x0 + DX, y1 + DY)],
                         closed=True, facecolor=c["top"], edgecolor=c["edge"], lw=1.3, zorder=2))
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor=c["front"],
                           edgecolor=c["edge"], lw=1.3, zorder=3))


def block(ax, cx, cy, w, h, c, text, fs=11):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    d = 0.06
    ax.add_patch(Polygon([(x1, y0), (x1, y1), (x1 + d, y1 + d), (x1 + d, y0 + d)],
                         closed=True, facecolor=c["side"], edgecolor=c["edge"], lw=1.0, zorder=5))
    ax.add_patch(Polygon([(x0, y1), (x1, y1), (x1 + d, y1 + d), (x0 + d, y1 + d)],
                         closed=True, facecolor=c["top"], edgecolor=c["edge"], lw=1.0, zorder=5))
    ax.add_patch(Rectangle((x0, y0), w, h, facecolor=c["front"], edgecolor=c["edge"],
                           lw=1.0, zorder=6))
    ax.text(cx, cy, text, ha="center", va="center", color="white", fontsize=fs,
            fontweight="bold", zorder=7, linespacing=1.4)


def band(ax, y0, y1, c, text, fs=11.5):
    ax.add_patch(FancyBboxPatch((LX, y0), RX - LX, y1 - y0,
                                boxstyle="round,pad=0.02,rounding_size=0.1",
                                facecolor=c["front"], edgecolor=c["edge"], lw=1.5, zorder=4))
    ax.text((LX + RX) / 2, (y0 + y1) / 2, text, ha="center", va="center",
            fontsize=fs, fontweight="bold", color="#222222", zorder=7)


def layer_title(ax, y, text):
    ax.text(TITLE_X, y, text, ha="center", va="center", fontsize=15,
            fontweight="bold", color=PAL["ink"], linespacing=1.4, zorder=9)


def vdouble(ax, x, y0, y1):
    off = 0.08
    ax.annotate("", xy=(x - off, y1), xytext=(x - off, y0),
                arrowprops=dict(arrowstyle="-|>", color=PAL["arrow"], lw=2.0,
                                mutation_scale=12), zorder=8)
    ax.annotate("", xy=(x + off, y0), xytext=(x + off, y1),
                arrowprops=dict(arrowstyle="-|>", color=PAL["arrow"], lw=2.0,
                                mutation_scale=12), zorder=8)


def vdown(ax, x, y0, y1):
    ax.annotate("", xy=(x, y1), xytext=(x, y0),
                arrowprops=dict(arrowstyle="-|>", color=PAL["arrow"], lw=2.2,
                                mutation_scale=13), zorder=8)


# --- icons (identical artwork to the English edition) ---
def icon_base(ax, cx, cy, s=1.0):
    w, h = 0.85 * s, 0.38 * s
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#222222", edgecolor="black", lw=1.1, zorder=6))
    for dx in (-w / 2 + 0.02, w / 2 - 0.12):
        for dy in (-h / 2 - 0.05, h / 2 - 0.05):
            ax.add_patch(Circle((cx + dx, cy + dy), 0.075 * s, facecolor="#111111",
                                edgecolor="black", lw=0.9, zorder=7))


def icon_arm(ax, cx, cy, s=1.0):
    p0 = (cx - 0.05 * s, cy - 0.26 * s)
    p1 = (cx + 0.02 * s, cy + 0.03 * s)
    p2 = (cx + 0.25 * s, cy + 0.22 * s)
    ax.add_patch(Circle(p0, 0.095 * s, facecolor="#222222", edgecolor="black", lw=1.1, zorder=6))
    ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color="#222222", lw=6 * s,
            solid_capstyle="round", zorder=6)
    ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color="#222222", lw=5 * s,
            solid_capstyle="round", zorder=6)
    ax.add_patch(Circle(p1, 0.065 * s, facecolor="#444444", edgecolor="black", lw=0.9, zorder=7))
    ax.plot([p2[0], p2[0] + 0.13 * s], [p2[1] + 0.02 * s, p2[1] + 0.11 * s],
            color="#222222", lw=2.8 * s, solid_capstyle="round", zorder=7)
    ax.plot([p2[0], p2[0] + 0.13 * s], [p2[1] - 0.02 * s, p2[1] - 0.11 * s],
            color="#222222", lw=2.8 * s, solid_capstyle="round", zorder=7)


def icon_head(ax, cx, cy, s=1.0):
    ax.add_patch(Rectangle((cx - 0.15 * s, cy - 0.28 * s), 0.3 * s, 0.11 * s,
                           facecolor="#222222", edgecolor="black", lw=1, zorder=6))
    ax.add_patch(Circle((cx, cy - 0.06 * s), 0.12 * s, facecolor="#333333",
                        edgecolor="black", lw=1.1, zorder=7))
    ax.add_patch(FancyBboxPatch((cx - 0.19 * s, cy + 0.04 * s), 0.38 * s, 0.2 * s,
                                boxstyle="round,pad=0.01,rounding_size=0.05",
                                facecolor="#222222", edgecolor="black", lw=1.1, zorder=7))
    ax.add_patch(Circle((cx, cy + 0.14 * s), 0.05 * s, facecolor="#777777",
                        edgecolor="black", lw=0.8, zorder=8))


def icon_d455(ax, cx, cy, s=1.0):
    w, h = 0.78 * s, 0.32 * s
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#222222", edgecolor="black", lw=1.1, zorder=6))
    for dx, col in ((-0.21, "#5B8DEF"), (0.0, "#333333"), (0.21, "#5B8DEF")):
        ax.add_patch(Circle((cx + dx * s, cy), 0.08 * s, facecolor=col,
                            edgecolor="#999999", lw=1.1, zorder=7))


def icon_mic(ax, cx, cy, s=1.0):
    ax.add_patch(FancyBboxPatch((cx - 0.085 * s, cy - 0.02 * s), 0.17 * s, 0.28 * s,
                                boxstyle="round,pad=0.01,rounding_size=0.07",
                                facecolor="#222222", edgecolor="black", lw=1, zorder=6))
    ax.plot([cx, cx], [cy - 0.02 * s, cy - 0.19 * s], color="#222222", lw=2.3 * s, zorder=6)
    ax.plot([cx - 0.1 * s, cx + 0.1 * s], [cy - 0.19 * s, cy - 0.19 * s],
            color="#222222", lw=2.3 * s, zorder=6)


def icon_display(ax, cx, cy, s=1.0):
    w, h = 0.58 * s, 0.4 * s
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2 + 0.05 * s), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.04",
                                facecolor="#222222", edgecolor="black", lw=1.1, zorder=6))
    ax.add_patch(Rectangle((cx - w / 2 + 0.05 * s, cy - h / 2 + 0.1 * s),
                           w - 0.1 * s, h - 0.1 * s,
                           facecolor="#3A6EA5", edgecolor="none", zorder=7))
    ax.add_patch(Rectangle((cx - 0.05 * s, cy - h / 2 - 0.05 * s), 0.1 * s, 0.09 * s,
                           facecolor="#222222", edgecolor="black", lw=0.8, zorder=6))


fig, ax = plt.subplots(figsize=(14, 10), dpi=200)
ax.set_xlim(0, 14)
ax.set_ylim(0, 10.1)
ax.axis("off")

ax.text(7, 9.82, "XLeRobot 养老服务机器人 — 系统架构",
        ha="center", va="center", fontsize=23, fontweight="bold", color=PAL["ink"])

band(ax, 9.05, 9.48, PAL["band_sup"],
     "生命周期：systemd 用户服务（xlerobot.service）+ demo_manager 监管  —  启动 · 监控 · 重启 · 停止",
     fs=12)

slab(ax, 7.45, 8.65, PAL["app"])
slab(ax, 5.95, 7.05, PAL["bus"])
slab(ax, 3.85, 5.55, PAL["svc"])
slab(ax, 1.35, 3.15, PAL["hal"])

layer_title(ax, 8.05, "应用层")
layer_title(ax, 6.50, "本地\nHTTP 总线")
layer_title(ax, 4.70, "服务层")
layer_title(ax, 2.25, "硬件层")

app_centers = [3.95, 6.70, 9.45, 12.20]
app_labels = [
    "语音对话\nvoice_auto_web · :8082",
    "健康站\nhealth_station · :8084",
    "摄像头网页\ncamera_web · :8080",
    "遥控 / 手柄\nteleop_joy",
]
for cx, label in zip(app_centers, app_labels):
    block(ax, cx, 8.05, 2.45, 0.95, PAL["app_box"], label, fs=10.5)

ax.text(8.4, 6.74, "robot_api.py  REST 接口（:8090） —  底盘 · 头部 · 双臂 · 跟随锁",
        ha="center", va="center", fontsize=13, fontweight="bold", color="#7A4410", zorder=9)
ax.annotate("", xy=(13.15, 6.42), xytext=(3.3, 6.42),
            arrowprops=dict(arrowstyle="<|-|>", color=PAL["bus"]["edge"], lw=5.5,
                            mutation_scale=20), zorder=8)
ax.text(8.4, 6.13, "仅本机访问的 JSON  ·  端口 8080 / 8082 / 8084 / 8086 / 8090",
        ha="center", va="center", fontsize=10.5, color="#7A4410", zorder=9)

svc_centers = [3.55, 5.90, 8.25, 10.60, 12.85]
svc_specs = [
    ("运动与双臂\nrobot_api", PAL["svc_box"]),
    ("头部跟踪\n+ 底盘跟随", PAL["svc_box"]),
    ("D455 视觉\nMediaPipe · 深度", PAL["svc_box"]),
    ("语音识别 / 合成\nFunASR · edge-tts", PAL["svc_box"]),
    ("ROS 2 Nav2\nRTAB-Map", PAL["ros_box"]),
]
for cx, (label, col) in zip(svc_centers, svc_specs):
    block(ax, cx, 4.70, 2.05, 1.05, col, label, fs=10.5)

for x in app_centers:
    vdouble(ax, x, 7.10, 7.40)
for x in svc_centers:
    vdouble(ax, x, 5.60, 5.90)
for x in svc_centers:
    vdown(ax, x, 3.80, 3.22)

hw_centers = [3.35, 5.30, 7.25, 9.20, 11.10, 12.90]
hw_specs = [
    (icon_base, "差速底盘\n两轮，5 英寸"),
    (icon_arm, "2 × SO-101 机械臂\nFeetech 总线"),
    (icon_head, "360° 头部\n偏航 / 俯仰"),
    (icon_d455, "RealSense D455\nRGB + 对齐深度"),
    (icon_mic, "麦克风 / 音箱\nPipeWire 回声消除"),
    (icon_display, "表情屏\n:8086"),
]
for cx, (draw_fn, caption) in zip(hw_centers, hw_specs):
    draw_fn(ax, cx, 2.45)
    ax.text(cx, 1.62, caption, ha="center", va="center", fontsize=10,
            fontweight="bold", color=PAL["ink"], linespacing=1.35, zorder=9)

band(ax, 0.25, 0.82, PAL["band_safe"],
     "代码级安全：语音“停下”硬切断  ·  跟随锁 + 0.6 秒速度租约  ·  "
     "1 米保持 / 0.85 米禁前进 / 0.55 米后退  ·  急停撤销跟随锁",
     fs=10.5)

out_base = os.path.join(_HERE, "xlerobot_architecture_cn")
plt.savefig(out_base + ".png", bbox_inches="tight", facecolor="white", pad_inches=0.18)
plt.savefig(out_base + ".svg", bbox_inches="tight", facecolor="white", pad_inches=0.18)
print("saved", out_base + ".png", "and", out_base + ".svg")
