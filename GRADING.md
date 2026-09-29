# TortillaBench grading

A vision-capable LLM grades each run. Give the judge the prompt below together with:

- `TASK.md`
- the run's final image (or `preview.jpg`) and each iteration render
- the final script (`build_tortilla.py`)
- the model's report (`REPORT.md` or similar)
- a file listing of the run folder

Save the judge's JSON into the run's `meta.json` as `"grade"`. The gallery shows `grade.overall`.

## Judge prompt

```text
You are judging a submission to TortillaBench. The spec is in TASK.md: a
headless Blender script that cloth-simulates and renders a photorealistic
flour tortilla folded into quarters on a wooden cutting board.

Look at the images yourself. Don't take the model's report at face value, but
do use it and the file listing to check the process rules.

Score each category from 0 to 10:
- realism: would this pass as a food photo at a glance?
- fold: two successive folds (quarters), soft rounded edges, layer separation,
  sitting on the board without floating or intersecting
- tortilla_material: off-white base, griddle char spots, flour dust, SSS, sheen
- board_material: directional grain, colour variation, knife marks, oiled finish
- lighting_camera: food-photo lighting, low angle, shallow DOF focused on the
  leading fold edge, clean composition
- iteration: did each render's critique identify real problems, and did the
  next render fix them?
- code: structure, logging, reproducibility, and following constraints C1-C6

Then give the pass/fail gates:
- ran_headless: the pipeline ran end-to-end from the CLI with no GUI
- render_budget: no more than 3 renders, judged from logs and files; bakes
  that produced no image are fine
- no_external_assets: everything is procedural; no HDRIs, textures or models

Reply with only this JSON:
{
  "scores": {"realism": 0, "fold": 0, "tortilla_material": 0, "board_material": 0,
             "lighting_camera": 0, "iteration": 0, "code": 0},
  "gates": {"ran_headless": true, "render_budget": true, "no_external_assets": true},
  "overall": 0.0,
  "verdict": "one or two sentences",
  "judge": "<judge model name>"
}

"overall" is the mean of the scores, rounded to one decimal, or 0 if any gate fails.
```
