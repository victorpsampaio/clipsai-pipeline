# 🎬 ClipsAI Pipeline

Pipeline automático para criar e publicar YouTube Shorts / TikTok / Instagram Reels a partir de vídeos longos do YouTube.

---

## O que faz

1. **Baixa** o vídeo do YouTube (pytubefix + yt-dlp com OAuth2)
2. **Transcreve** com WhisperX (GPU, modelo Whisper)
3. **Detecta clips** virais com ClipsAI (embeddings semânticos)
4. **Exporta** em formato vertical 1080×1920 com:
   - Legendas karaoke word-by-word (estilo TikTok)
   - Crop dinâmico com face tracking (OpenCV Haar Cascade)
   - Title card animado (fade in/out)
   - Música de fundo automática (Jamendo CC)
5. **Analisa** com LLM (Groq) → título, legenda, hashtags, score de viralidade
6. **Publica** no YouTube Shorts (e Instagram Reels)

---

## Pré-requisitos do sistema

| Requisito | Versão mínima | Notas |
|---|---|---|
| Python | 3.11+ | Testado em 3.13 |
| Node.js | 20+ | Necessário para yt-dlp resolver n-challenge |
| ffmpeg | qualquer recente | Deve estar no PATH |
| GPU NVIDIA | CUDA 12.x | Recomendado; CPU funciona mas é lento |

### Instalar ffmpeg (Windows)

Baixe em [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) → `ffmpeg-release-essentials.zip`
Extraia e adicione a pasta `bin/` ao PATH do Windows.

### Instalar Node.js

Baixe em [nodejs.org](https://nodejs.org/) → LTS (v20+)

---

## Instalação

### 1. Criar ambiente virtual

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 2. Instalar PyTorch com CUDA

> ⚠️ Instale o PyTorch **antes** do `requirements.txt`. A versão depende da sua GPU/CUDA.

Verifique sua versão CUDA: `nvidia-smi`

**CUDA 12.6 (Python 3.13):**
```powershell
# Baixe os wheels em https://download.pytorch.org/whl/cu126
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
```

**CPU only (sem GPU):**
```powershell
pip install torch torchvision torchaudio
```

### 3. Instalar dependências

```powershell
pip install -r requirements.txt
```

---

## Variáveis de ambiente (opcional)

Crie um arquivo `.env` ou configure as variáveis antes de rodar:

```powershell
# Chave Groq (LLM) — gratuita em groq.com
$env:GROQ_API_KEY = "gsk_..."

# Credenciais Instagram (opcional)
$env:IG_APP_ID = "..."
$env:IG_APP_SECRET = "..."
```

---

## Configuração de credenciais

### YouTube (upload)

1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. Crie um projeto → ative a **YouTube Data API v3**
3. Crie credenciais OAuth 2.0 → Desktop App
4. Baixe como `client_secrets.json` e coloque na raiz do projeto
5. Na primeira execução, um browser abre para autorizar → token salvo em `youtube_token.json`

### Cookies do YouTube (download)

Para baixar vídeos sem bot detection:

1. Instale a extensão Chrome **"Get cookies.txt LOCALLY"**
2. Abra [youtube.com](https://youtube.com) logado na sua conta
3. Clique na extensão → **Export** → salve como `cookies.txt` na raiz do projeto

> ⚠️ Os cookies expiram. Re-exporte quando aparecer erro de autenticação.

### yt-dlp OAuth2 (alternativo)

```powershell
.venv\Scripts\yt-dlp --username oauth2 --password "" "https://www.youtube.com/watch?v=QUALQUER_ID"
# Abre um link para autorizar no browser — depois funciona sem cookies
```

---

## Uso

### Processar um vídeo

```powershell
.venv\Scripts\python run_clipsai.py \
  --url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out_dir output\VIDEO_ID \
  --device cuda \
  --model small \
  --vertical 1080x1920 \
  --karaoke --karaoke-style red --karaoke-position center \
  --title-card \
  --dynamic-crop \
  --llm-enhance --groq-key "gsk_..." \
  --cookies cookies.txt
```

### Postar clips no YouTube

```powershell
.venv\Scripts\python post_clips.py output\VIDEO_ID\*.clips.json \
  --youtube \
  --min-score 7
```

### Batch automático (múltiplos vídeos)

Crie `urls.txt` com uma URL por linha:
```
https://www.youtube.com/watch?v=ID1
https://www.youtube.com/watch?v=ID2
# linhas com # são ignoradas
```

Rode:
```powershell
.venv\Scripts\python batch_pipeline.py urls.txt \
  --youtube \
  --min-score 7 \
  --karaoke --karaoke-style red --karaoke-position center \
  --title-card \
  --dynamic-crop \
  --llm-enhance --groq-key "gsk_..." \
  --auto-bgm \
  --cookies cookies.txt
```

O progresso é salvo em `batch_state.json` — interrompa e retome sem reprocessar.

---

## Flags principais

### run_clipsai.py

| Flag | Descrição |
|---|---|
| `--url` | URL do YouTube |
| `--input` | Arquivo de vídeo local (pula download) |
| `--model` | Modelo Whisper: `tiny`, `small`, `medium`, `large` |
| `--vertical 1080x1920` | Exporta em formato vertical para Shorts |
| `--karaoke` | Legendas word-by-word estilo TikTok |
| `--karaoke-style` | `yellow` `white` `red` `green` |
| `--karaoke-position` | `bottom` `center` `top` |
| `--title-card` | Título animado nos primeiros 2s |
| `--dynamic-crop` | Face tracking automático |
| `--llm-enhance` | Gera título/legenda/hashtags com Groq |
| `--groq-key` | Chave da API Groq |
| `--bgm-dir` | Pasta com música de fundo (.mp3/.wav) |
| `--cookies` | Arquivo cookies.txt para autenticação |
| `--cache-dir` | Cache de vídeos (evita re-download) |

### batch_pipeline.py (flags extras)

| Flag | Descrição |
|---|---|
| `--auto-bgm` | Baixa música adequada ao tema automaticamente (Jamendo CC) |
| `--min-score N` | Só posta clips com virality_score ≥ N |
| `--youtube` | Posta no YouTube Shorts após clipar |
| `--instagram` | Posta no Instagram Reels após clipar |
| `--dry-run` | Mostra comandos sem executar |

---

## Estrutura do projeto

```
ClipsAI-Pipeline/
├── run_clipsai.py        # Pipeline principal (download → transcrição → clips → export)
├── post_clips.py         # Upload para YouTube / Instagram
├── batch_pipeline.py     # Orquestrador batch (múltiplos vídeos)
├── enhance_clips.py      # Rodar só o LLM em clips já processados
├── auto_bgm.py           # Download automático de música de fundo (Jamendo)
├── requirements.txt      # Dependências pip (sem PyTorch)
├── urls.txt              # Lista de URLs para processar (não versionado se sensível)
│
├── output/               # Clips gerados (não versionado)
│   └── VIDEO_ID/
│       ├── clip000.mp4
│       ├── clip000.jpg   # thumbnail
│       ├── clip000.ass   # legendas karaoke
│       └── *.clips.json  # metadados
│
├── bgm/                  # Músicas de fundo (não versionado)
│
# Credenciais — NÃO versionar:
├── client_secrets.json
├── youtube_token.json
├── cookies.txt
└── .env
```

---

## Solução de problemas

### yt-dlp: "n challenge solving failed"
```powershell
# Certifique-se que Node.js está instalado e no PATH
node --version  # deve retornar v20+
pip install yt-dlp-ejs
```

### yt-dlp: "Sign in to confirm you're not a bot"
Re-exporte o `cookies.txt` do Chrome (veja seção de configuração acima).

### pytubefix: "player_ias.vflset"
```powershell
pip install -U pytubefix
```

### mediapipe: "has no attribute 'solutions'"
Versão 0.10+ removeu o módulo `solutions`. O pipeline já usa OpenCV Haar Cascade como fallback automático — nenhuma ação necessária.

### YouTube: "uploadLimitExceeded"
Limite diário de uploads atingido. Acesse [youtube.com/verify](https://youtube.com/verify) para aumentar o limite. Reseta à meia-noite PT (~4h BRT).

### Memória insuficiente (paging file too small)
Aumente o arquivo de paginação do Windows:
`Win+R → sysdm.cpl → Avançado → Desempenho → Configurações → Avançado → Memória Virtual`
Configure mínimo 8GB e máximo 16GB.

---

## Licença de música automática

Quando usado `--auto-bgm`, as faixas são baixadas do [Jamendo](https://www.jamendo.com) sob licença Creative Commons.
Consulte a licença específica de cada faixa em jamendo.com.
