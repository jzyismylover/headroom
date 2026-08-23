#!/usr/bin/env python3
"""Post-process the pipeline sequence diagram: actor size + highlight colors + legend."""
import re

p = "drawio/headroom-pipeline-seq.drawio"
s = open(p, encoding="utf-8").read()

# ---- 1. Actor figure: bump header size 40 -> 64 (fix flat/squished figure) ----
s = s.replace(
    "portConstraint=eastwest;size=40;participant=umlActor;verticalAlign=bottom;spacingBottom=-14;",
    "portConstraint=eastwest;size=64;participant=umlActor;verticalAlign=bottom;spacingBottom=-16;",
)

# ---- 2. Lifeline header theme colors ----
lifeline_styles = {
    "agent":  "fillColor=#dae8fc;strokeColor=#6c8ebf;fontColor=#1a3a5c;fontStyle=1;",
    "proxy":  "fillColor=#ffe6cc;strokeColor=#d79b00;fontColor=#7a4d00;fontStyle=1;",
    "pipe":   "fillColor=#fff2cc;strokeColor=#d6b656;fontColor=#7a6200;fontStyle=1;",
    "router": "fillColor=#dae8fc;strokeColor=#6c8ebf;fontColor=#1a3a5c;fontStyle=1;",
    "comp":   "fillColor=#d5e8d4;strokeColor=#82b366;fontColor=#2f5c2f;fontStyle=1;",
    "mem":    "fillColor=#e1d5e7;strokeColor=#9673a6;fontColor=#4d3560;fontStyle=1;",
    "up":     "fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;fontStyle=1;",
}
for cid, extra in lifeline_styles.items():
    m = re.search(r'(<mxCell id="%s" parent="1" style="[^"]*)" value=' % cid, s)
    assert m, f"lifeline {cid} not found"
    s = s[: m.start(1)] + m.group(1) + extra + s[m.end(1):]

# ---- 3. Activation bar fills (theme-matched) ----
bar_fills = {
    "act0": ("#ffe6cc", "#d79b00"),
    "act1": ("#fff2cc", "#d6b656"),
    "act2": ("#dae8fc", "#6c8ebf"),
    "act3": ("#d5e8d4", "#82b366"),
    "act4": ("#e1d5e7", "#9673a6"),
    "act5": ("#f5f5f5", "#666666"),
}
for bid, (fill, stroke) in bar_fills.items():
    m = re.search(r'(<mxCell id="%s" parent="\w+" style="html=1;points=\[\]\;perimeter=orthogonalPerimeter;outlineConnect=0;fillColor=#ffffff;)"' % bid, s)
    assert m, f"bar {bid} not found"
    s = s[: m.start(1)] + m.group(1).replace(
        "fillColor=#ffffff;", f"fillColor={fill};strokeColor={stroke};"
    ) + s[m.end(1):]

# ---- 4. Highlight key message edges ----
# id -> (strokeColor, fontColor)
hl = {
    "m7":  ("#6c8ebf", "#1a3a5c"),  # INPUT_CACHED      -> blue  (semantic cache)
    "m8":  ("#82b366", "#2f5c2f"),  # route()           -> green (compression core)
    "m9":  ("#82b366", "#2f5c2f"),  # INPUT_ROUTED      -> green
    "m10": ("#82b366", "#2f5c2f"),  # compress()        -> green
    "m11": ("#82b366", "#2f5c2f"),  # INPUT_COMPRESSED  -> green
    "m12": ("#82b366", "#2f5c2f"),  # TOOLS_COMPRESSED  -> green
    "m13": ("#9673a6", "#4d3560"),  # extract+write     -> purple (memory)
    "m14": ("#9673a6", "#4d3560"),  # INPUT_REMEMBERED  -> purple
}
for mid, (stroke, font) in hl.items():
    m = re.search(
        r'(<mxCell id="%s" edge="1" parent="1" source="\w+" style="html=1;verticalAlign=bottom;endArrow=block;curved=0;rounded=0;)' % mid,
        s,
    )
    assert m, f"edge {mid} not found"
    s = s[: m.start(1)] + m.group(1) + (
        f"strokeColor={stroke};strokeWidth=2.5;fontColor={font};fontStyle=1;labelBackgroundColor=#ffffff;"
    ) + s[m.end(1):]

# ---- 5. Legend (bottom, clear of lifelines which end at y=1300) ----
legend = """
        <mxCell id="legend" value="&lt;b&gt;图例&lt;/b&gt;" style="rounded=0;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#666666;verticalAlign=top;fontSize=11;fontStyle=1;" vertex="1" parent="1">
          <mxGeometry x="640" y="1315" width="170" height="120" as="geometry" />
        </mxCell>
        <mxCell id="leg1" value="" style="rounded=0;html=1;fillColor=#82b366;strokeColor=#82b366;" vertex="1" parent="legend">
          <mxGeometry x="12" y="32" width="30" height="16" as="geometry" />
        </mxCell>
        <mxCell id="leg1t" value="压缩核心（路由/压缩/工具）" style="text;html=1;align=left;verticalAlign=middle;fontSize=10;" vertex="1" parent="legend">
          <mxGeometry x="50" y="30" width="112" height="20" as="geometry" />
        </mxCell>
        <mxCell id="leg2" value="" style="rounded=0;html=1;fillColor=#6c8ebf;strokeColor=#6c8ebf;" vertex="1" parent="legend">
          <mxGeometry x="12" y="58" width="30" height="16" as="geometry" />
        </mxCell>
        <mxCell id="leg2t" value="语义缓存（INPUT_CACHED）" style="text;html=1;align=left;verticalAlign=middle;fontSize=10;" vertex="1" parent="legend">
          <mxGeometry x="50" y="56" width="112" height="20" as="geometry" />
        </mxCell>
        <mxCell id="leg3" value="" style="rounded=0;html=1;fillColor=#9673a6;strokeColor=#9673a6;" vertex="1" parent="legend">
          <mxGeometry x="12" y="84" width="30" height="16" as="geometry" />
        </mxCell>
        <mxCell id="leg3t" value="记忆（extract/remembered）" style="text;html=1;align=left;verticalAlign=middle;fontSize=10;" vertex="1" parent="legend">
          <mxGeometry x="50" y="82" width="112" height="20" as="geometry" />
        </mxCell>
"""
s = s.replace("      </root>", legend + "\n      </root>")

open(p, "w", encoding="utf-8").write(s)
print("post-processed:", p)
