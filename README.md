# 🌮 TortillaBench

**Can your model fold a tortilla?**

One task: using only headless Blender and one Python script, build, cloth-simulate, shade, light, and render a photorealistic warm flour tortilla folded into quarters on a wooden cutting board. No GUI, no external assets, no HDRIs, and only **three renders** to get it right.

It's a fun benchmark covering 3D modeling, physics, procedural materials, photography, and the model's ability to critique its own work. The full spec is in [`TASK.md`](TASK.md).

## Gallery

<!-- gallery:start -->
| Final render | Model | Date | Grade | Run |
|---|---|---|---|---|
| <img src="results/claude-sonnet-5.5/2026-09-29/preview.jpg" width="360"> | **Claude Sonnet 5.5**<br>claude-code | 2026-09-29 | not graded | [files](results/claude-sonnet-5.5/2026-09-29) |
<!-- gallery:end -->

## Run it on a model

You need [Blender](https://www.blender.org/download/) (latest stable) and Python 3.8+. No other dependencies are needed.

Each run happens in its **own workspace outside this repo**, so the model can't peek at other models' tortillas.

```bash
# 1. Make an isolated workspace (contains only TASK.md)
python tortilla.py new "claude-opus-5-5"
# -> ~/tortillabench-runs/claude-opus-5-5-2026-09-29

# 2. Start your agent *in that folder* and give it the prompt below

# 3. Copy the finished run into results/ and rebuild the gallery
python tortilla.py collect ~/tortillabench-runs/claude-opus-5-5-2026-09-29 --model "Claude Opus 5.5" --harness claude-code
python tortilla.py gallery
```

### Agent prompt

```text
Read TASK.md in this folder. It is the complete spec. Complete the task,
keeping every file you create inside this folder, and finish by writing
REPORT.md covering all the deliverables.
```

That's it: there's no harness and no enforcement. The model uses its own tools however it likes, and everything it leaves behind gets graded. Whether it honestly stuck to the 3-render budget is part of what the grader reads for.

`collect` copies the whole workspace. It skips `.blend1` backups, byte-identical duplicates, and all but the newest `.blend`. It then writes `meta.json` and a small `preview.jpg` for the gallery. It guesses the final render (the newest full-size image); use `--final path/to/image.png` if it guesses wrong.

## Grading

Runs are graded later by an LLM judge using [`GRADING.md`](GRADING.md). The judge gets the final render, the iteration renders, the script, and the report, and its scores go in the `grade` field of `meta.json`.

## Submitting results

Open a PR that adds your `results/<model>/<date>/` folder, exactly as `collect` produced it. Failed runs are welcome too; the disasters are half the fun.

## License

[MIT](LICENSE). This covers everything: the task, the scripts, and the results. Fork it, remix it, and put it in your paper or your tweet.
