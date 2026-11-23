# Architecture diagrams

Original system-architecture artwork for the XLeRobot elder-care robot, drawn
from the project's real service map (see `../../README.md`).

| File | Language |
|---|---|
| `xlerobot_architecture.png` / `.svg` | English |
| `xlerobot_architecture_cn.png` / `.svg` | Chinese |
| `draw_xlerobot_arch.py` | English source (matplotlib) |
| `draw_xlerobot_arch_cn.py` | Chinese source (matplotlib) |

## Regenerate

```bash
python3 docs/images/draw_xlerobot_arch.py      # English
python3 docs/images/draw_xlerobot_arch_cn.py   # Chinese
```

Outputs are written next to the scripts.

### Chinese font note

The system Noto CJK is a `.ttc` whose first face is Japanese, so matplotlib
picks JP glyphs. For the simplified-Chinese edition, extract the SC face once:

```bash
python3 - <<'EOF'
from fontTools.ttLib import TTCollection
for src, dst in [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "docs/images/fonts/NotoSansCJKsc-Regular.otf"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
     "docs/images/fonts/NotoSansCJKsc-Bold.otf"),
]:
    coll = TTCollection(src)
    for font in coll.fonts:
        if (font["name"].getDebugName(1) or "").strip() == "Noto Sans CJK SC":
            font.save(dst)
            print("saved", dst)
            break
EOF
```

The script also accepts pre-extracted fonts under `/tmp/opencode/fonts/`, and
falls back to the system TTC (JP face) if neither exists.
