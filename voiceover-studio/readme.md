# OpenTTS

OpenTTS is a local voice studio for Apple Silicon. It uses [Qwen3-TTS](https://huggingface.co/collections/Qwen/qwen3-tts) through FusedRender's `fused.ai.speech`, and Whisper or Apple speech through `fused.ai.transcribe`. The app loads no models itself. All models operate on this Mac. The app needs no API keys. No audio goes off the Mac.

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
    - A local transcription model (Whisper or Apple on-device speech) writes the transcript. You can select the model and run it again. Save is available only when the transcript is complete.
    - The app fills in the description (pitch, pace, room) and the language.
  - **Design a voice**: Describe the voice, for example "warm baritone, light British accent". Qwen3-TTS VoiceDesign makes the voice. Save a preview that you like. You can then use it again.
  - **Presets**: The 9 built-in CustomVoice speakers. They accept style instructions.

**History** is a side panel that you can open and close. For each take, you can play, download, make a new take, or do an **Eval**. Eval makes a transcript of the audio with a local transcription model. Then it shows the word and character error rates against the script.

## Models

FusedRender supplies all models. The app takes the model list from `fused.ai.models.catalog()`, so it shows the models that your FusedRender version has.

| Model | Use | Download |
|---|---|---|
| Qwen3-TTS Base 1.7B / 0.6B | Cloned and designed voices | 4.5 / 2.5 GB |
| Qwen3-TTS CustomVoice 1.7B / 0.6B | Preset speakers | 4.5 / 2.5 GB |
| Qwen3-TTS VoiceDesign 1.7B | Voices from a description | 4.5 GB |
| Whisper (tiny to large-v3) | Transcripts of voice samples and Eval | 0.05–3.1 GB |
| Apple on-device speech | Transcripts, macOS 26 and later | built in |

Each model downloads one time. Other FusedRender apps and the AI Playground share the same files. If the model for your voice is not on this Mac, a **Confirm download** button shows beside the engine size. Click Generate, then click Confirm download. The download manager shows the progress. The generation and the mix run in the app's background worker. You can go to a different app. The take shows in History when it is complete. Each voice can use a fixed engine size. "Auto" uses the size that is on disk. FusedRender keeps one speech model in memory at a time. Thus a script that changes between a preset voice and a cloned voice loads a model again at each change.

Most transcription models write false text for silence, for example "Thank you.". Thus the app refuses silent recordings before the transcript.

Requirements: Apple Silicon and FusedRender 0.6.3 or later.

## Data

Voices, takes and the audio of each line are in `.fused/data/`. That folder is local to this Mac. The export does not include it.
