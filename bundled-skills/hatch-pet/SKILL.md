---
name: hatch-pet
description: Create, repair, validate and publish animated MiniCode pets from a concept or reference art, with nine standard animation rows and sixteen look directions.
---

# Hatch Pet

Create a complete v2 pet for MiniCode's Mini window. The final transparent PNG or WebP atlas is exactly 1536×2288, with eight columns, eleven rows and 192×208 cells. Preserve the user's intended style and the same pet identity across the full animation.

Read [the generation and QA workflow](references/hatch-workflow.md) before starting. It includes the row contract, reference grounding, deterministic processing commands, look-direction review, independent visual worker prompts and repair workflow. Read [the pet contract](references/codex-pet-contract.md) when inspecting an existing pet.

Call `hatch_pet` with `action: "runtime"` to obtain the actual local Python/Pillow interpreter and this installed Skill directory. All work stays in the accepted conversation workspace. The workflow's shell examples use these returned values; translate shell syntax to PowerShell when that is the selected shell.

Use `generate_image` for visual generation. Every row request supplies every listed `input_images`, including the same approved base image and its layout guide. Set a fresh `output_path` for each generated result so the deterministic scripts can consume the actual bytes. Do not replace missing generated rows with drawings or transforms from another state. Only the documented running-left mirror derivation is allowed.

After deterministic validation and independent visual QA pass, call `hatch_pet` with `action: "publish"` and `run_directory`. This writes the completed pet into MiniCode's private pets folder and notifies the desktop to refresh and select it. Publication confirms the saved files; the desktop reports any selection error separately. Do not write into Codex's user folders or install cloud plugins. Keep the run's generated sources and review evidence available for later repair.
