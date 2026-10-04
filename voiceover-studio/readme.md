# OpenTTS

OpenTTS is a local voice studio for Apple Silicon. It uses [Qwen3-TTS](https://huggingface.co/collections/Qwen/qwen3-tts) through [mlx-audio](https://github.com/Blaizzy/mlx-audio). All models operate on this Mac. The app needs no API keys. No audio goes off the Mac.

## Workspaces

- **Text to speech**: Write or upload a script. Select a voice from the menu. Then click Generate. History keeps all takes.
  - **Upload script** accepts plain text, Markdown, SRT, VTT, SBV, LRC, ASS/SSA, Whisper-style JSON, CSV/TSV transcripts, RTF and Word (.docx). The app removes timestamps and speaker labels and keeps the text.
  - The **speech model** menu beside the language shows the exact Qwen3-TTS model for the selected voice, its Hugging Face id, its size and its state. A model that is not on this Mac has a Download button in that menu.
- **Dubbing**: Import an `.srt` or `.vtt` file. Each cue becomes a line at its timestamp.
  - The app finds speaker labels (`ALEX:`, `[Alex]`, VTT `<v Alex>`). You can give one voice to each speaker.
  - You can generate one line or all lines. You can adjust start times, add lines and delete lines. Then mix.
  - A line that is longer than its subtitle slot shows in red.
- **Voices**: Your voice library.
  - **Clone your voice**: Record with the microphone, or upload a sample of 10–30 s.
    - The app records through the browser microphone. Select the input device below the record button. The level meter shows the signal. If no sound comes in, a warning shows.
    - The app removes silence and sets the level. It examines the length, loudness, clipping and background noise.
    - The app refuses a silent recording. A model writes false text for silence, for example "Thank you.".
    - A local speech model writes the transcript. You can select the model and run it again. Save is available only when the transcript is complete.
    - The app fills in the description (pitch, pace, room) and the language.
  - **Design a voice**: Describe the voice, for example "warm baritone, light British accent". Qwen3-TTS VoiceDesign makes the voice. Save a preview that you like. You can then use it again.
  - **Presets**: The 9 built-in CustomVoice speakers. They accept style instructions.

**History** is a side panel that you can open and close. For each take, you can play, download, make a new take, or do an **Eval**. Eval makes a transcript of the audio with a local speech model. Then it shows the word and character error rates against the script.

## Models

| Model | Use | Download |
|---|---|---|
| Qwen3-TTS Base 1.7B / 0.6B | Cloned and designed voices | 4.5 / 2.5 GB |
| Qwen3-TTS CustomVoice 1.7B / 0.6B | Preset speakers | 4.5 / 2.5 GB |
| Qwen3-TTS VoiceDesign 1.7B | Voices from a description | 4.5 GB |
| Qwen3-ASR 1.7B (default) / 0.6B | Transcripts of voice samples | 2.5 / 1.0 GB |
| Parakeet TDT 0.6B v3 | Transcripts, 25 European languages | 2.5 GB |
| Whisper (FusedRender) / Apple on-device speech | Transcripts, optional | 0.05–3.1 GB / built in |

Each model downloads one time into the Hugging Face cache. If the model for your voice is not on this Mac, a **Confirm download** button shows beside the engine size. Click Generate, then click Confirm download. The download, the generation and the mix all run in the background worker. You can go to a different app. The take shows in History when it is complete. The download manager shows the progress. You can manage the models in the Engines menu. Each voice can use a fixed engine size. "Auto" uses the size that is on disk. The loaded models stay in memory in a background worker. The worker stops after 15 minutes with no use.

## Transcription test

Test clips: two human voice samples, one synthetic sample, one laptop-microphone simulation, one noisy clip and 10 s of silence.

| Model | Clean clips (WER) | Laptop microphone | Very noisy | Silence |
|---|---|---|---|---|
| Qwen3-ASR 1.7B | 0% | 0% | 66% | "I'm not sure." |
| Qwen3-ASR 0.6B | 0% | 0% | 88% | "The." |
| Parakeet TDT 0.6B v3 | 0–3% | 0% | 53% | no text |
| Whisper large-v3 turbo | 0% | 0% | 81% | "Thank you." |
| Apple on-device speech | 0% | 3% | not tested | not tested |

Most models write false text for silence. Thus the app refuses silent recordings before the transcript. Parakeet is the fastest model and the best model for noisy audio. But it does not support Chinese, Japanese or Korean. Thus Qwen3-ASR 1.7B is the default.

## Data

Voices, takes and the audio of each line are in `.fused/data/`. That folder is local to this Mac. The export does not include it.
