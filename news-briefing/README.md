# Puppet Feeds Local

Pick up to 5 topics (Chips, Crypto, Tech market, AI, Rates & economy, Trade & geopolitics, Energy, EVs & batteries, Gaming, Ordinals & NFTs) and get:

- **The feed:** the latest headlines from Google News for all your topics, merged into one list. Duplicate stories are combined, and stories that touch more than one topic are marked **⇄ Crossover**.
- **The briefing:** written by **the AI model on your own computer**, through LM Studio or Ollama. It includes:
  - the big picture
  - a mood rating for each topic
  - **How it connects**, the storylines that link your topics
  - **What to watch**, scenarios with likelihood, timeframe and signals to look out for
  - **Coming up**, upcoming dated events

  Every point links to the stories it used.

It's **free**: no accounts, no API keys, and nothing to pay. The only things it fetches from the internet are the headlines and crypto prices.

> ### ⚠️ You need a free local AI app for the briefings
> The briefing is written by an AI model running **on your own computer**, not in the cloud. Before you start, install **one** of these free apps and download a model in it (step 2 below).
> The news feed works without one; you just won't get briefings.

## Setup

### 1. Install Python (free)

- **Windows:** run `winget install Python.Python.3.13` in Terminal, or download it from [python.org/downloads](https://www.python.org/downloads/).
- **Linux:** already installed.

Nothing else needs installing; the app only uses Python's built-in tools.

### 2. Install a free local AI app and a model

Pick **one**:

| App | Download | Best for |
|---|---|---|
| **LM Studio** | [lmstudio.ai](https://lmstudio.ai) (Windows, Mac, Linux) | Beginners: point-and-click model downloads |
| **Ollama** | [ollama.com/download](https://ollama.com/download) (Windows, Mac, Linux) | People happy with one terminal command |

Both are free. Then choose a model that suits your computer. Bigger models write better briefings but need more memory:

| Your computer | Model to download | In Ollama |
|---|---|---|
| Graphics card with **24 GB+** of memory (e.g. RTX 3090/4090/5090) | **Qwen3.8 27B** | `ollama pull qwen3.8:27b` |
| Graphics card with **8–16 GB** (most gaming PCs) | **Qwen3 8B**, or **Qwen3 14B** with 12 GB+ | `ollama pull qwen3:8b` or `ollama pull qwen3:14b` |
| No graphics card / laptop with 8–16 GB RAM | **Qwen3 4B**, which is slower but works | `ollama pull qwen3:4b` |

You can browse other models at [ollama.com/library](https://ollama.com/library), or in LM Studio's **Explore** tab: search for the model name and click Download.

LM Studio needs 16 GB of RAM (recommended) and a CPU with AVX2 support; Macs need Apple Silicon ([LM Studio system requirements](https://lmstudio.ai/docs/app/system-requirements)).

### 3. Switch on the AI app's local server

- **LM Studio:** go to **Settings → Local Model API** and switch on the **Local API server**. Also turn on **Just-in-time model loading**, so the model loads by itself when this app asks for it. The app finds LM Studio on port 1234 or 1235.
- **Ollama:** nothing to do. Ollama's server runs in the background as soon as it's installed (port 11434).

### 4. Start Puppet Feeds Local

- **Windows:** double-click **Start News Briefing.bat**. Don't open `app.html` directly; it only works when started this way.
- **Linux:** `bash run-news-briefing.sh`

It opens in your browser at `http://127.0.0.1:8765`. Pick your topics, choose your model from the **Local AI** list, and click **Write briefing**. To stop it, click **Close app** or close the small window that starts with it.

## Good to know

- **Speed:** depends on your model and graphics card. A 27B model on a decent GPU takes about 30 seconds to 2 minutes, and smaller models (7–8B) are quicker. The first run is slower while the model loads.
- **Saved briefings:** each briefing is kept for an hour, even if you restart the app, so returning to the same topics is instant. Click **Rewrite** for a fresh one.
- **"Too long for the context window" error:** in LM Studio, load the model with a context length of **8192 or more**. With Ollama, the app requests 8192 automatically.
- **Privacy:** the app only accepts connections from your own computer, and other websites can't use it or shut it down.
- **Predictions:** the "What to watch" section is scenarios to keep an eye on, not predictions, and nothing in it is financial advice. Local models can make mistakes, so check the linked stories before relying on anything.
