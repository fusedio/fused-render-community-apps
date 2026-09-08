# Local Image

![Local Image — a prompt panel beside a generated image](preview.png)

**Text-to-image with FLUX.2 Klein, generated entirely on your machine.** Type
a prompt, hit Generate, and watch the render come together — no cloud API,
no account, no key.

- **Advanced controls** for width, height, steps, guidance scale, and seed
  (with a randomize button).
- **Live progress** while the model downloads or denoises, with a Cancel
  button that actually stops the job.
- Every generated image is saved to disk; the path, seed, and settings used
  are shown once it's done, so a result is easy to reproduce.

The model is curated and held resident by fused-render's `fused.ai.image(...)`
call — this app has no Python dependencies of its own.
