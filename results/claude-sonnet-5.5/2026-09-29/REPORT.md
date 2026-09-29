# PROCEDURAL-PHOTOREAL-01 report

**Command (D2):** `blender --background --factory-startup --python build_tortilla.py`
Blender 4.5.10 LTS, AMD 16-thread CPU, NVIDIA RTX 5070 Ti (Cycles OptiX). Final run 120 s total (93 s render, ~30 s cloth).

**Files:** D1 `build_tortilla.py` | D3 `output/tortilla_render.png` (3840x2160, 16-bit) | D4 `output/tortilla_scene.blend`
D5 `logs/` (`render_01.log`=`iterations/render1.log`, `render_02.log`, `render_03_final.log`, plus `bake_test_*`/`tune*` logs) | D6 `iterations/`

## Render budget: 3 of 3 used
Cloth-only tuning runs (`--bake-only`, no image, allowed by I4) were logged: ~15 runs, including a 40+ min bake that I killed.

| # | Script | Image | Critique | Change |
|---|--------|-------|----------|--------|
| 1 | `build_tortilla_v1.py` | `render1.png` | Camera at 10 degrees showed only the fold side, no top surface, so no griddle spots. Overexposed, glossy coat washed out the wood. Tortilla looked waxy/pillowy, with a stepped block artifact at the fold (likely a solidify spike from a cloth outlier vertex; I fixed outliers afterwards but did not confirm the cause). The script ended "FAILED" only because the relative `--out-dir` resolved to `C:\output`; the image was rendered. | Absolute out-dir. Outlier-vertex repair and non-even solidify. Camera 85 mm at 17 degrees. Lower exposure and coat. Tighter layer spacing. Spot and bump changes. |
| 2 | `build_tortilla_v2.py` | `render2.png` | Composition and exposure OK, tortilla reads as a folded flatbread. Spots far too small (Voronoi scale was tuned for the wrong size). Wood grain looked like blurry water ripples. Lighting flat. | Spot scales x2.5, more contrast and toasted mottling. Grain warp and distortion reduced. Rim light up, fill down, darker backdrop. Camera 15 degrees. |
| 3 (final) | `build_tortilla.py` = `build_tortilla_v3.py` | `render3_final.png` | See below. | none (budget exhausted) |

## Honest assessment of the final image
- **Good:** it runs unattended; the cloth folds are physical (two hook-driven folds, stretch about 1.0, 0 self-intersections, lowest point seated on the board); DOF focuses on the leading fold edge; the tortilla colour is convincing; AgX exposure is reasonable.
- **Weak:**
  - Griddle spots are still sparse and low-contrast, and the flour dusting is barely visible.
  - The fold-2 leading edge sags in the middle.
  - The wood reads as regular stripes, not natural grain, and the scratches are invisible.
  - The lighting is soft and flat, with little rim definition.
  - There is no steam.
  - It falls well short of "photorealistic".

## Technical summary (D7)
- **Geometry:** 65x65 quad grid mapped to a disc (23 cm, irregular rim from summed sinusoids, so it stays quad-only), 1.5-2.1 mm thick via solidify with a noise weight map, plus subsurf. Board 50x32x2.4 cm with a 4.5 mm bevel; cyclorama backdrop.
- **Simulation:** cloth run at 6x scale (self-collision at real scale was unstable), scaled back after the bake. Two empties animated with hook modifiers drive pinned outer strips, so fold 1 (x>cx over about frames 1-46) then fold 2 (y>cy over about frames 68-114) fold successively. A soft-pinned static quadrant stops the base sliding. The frame-170 result is frozen into a static mesh, the board contact is clamped, and the baked simulation is kept hidden in the .blend.
- **Shading:** all procedural nodes. Tortilla: warped-UV Voronoi spots at three scales, toasted rim, flour specks, bump, weak SSS and sheen. Wood: warped Wave bands, streak noise, scratch sets, and an oil coat.
- **Lighting and camera:** key, fill and rim area lights sized from target irradiance; procedural gradient world. 85 mm at f/4 with 8 blades, focus on the leading fold edge, 15 degrees elevation.
- **Render:** Cycles, GPU with CPU fallback, 512 samples adaptive, OIDN, AgX, seed 1337, 16-bit PNG.

Reproducibility across repeated runs was not tested (seeds are fixed everywhere, but I ran the full pipeline once per iteration only).
