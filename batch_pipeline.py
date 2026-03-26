"""
batch_pipeline.py — Esteira automática: lista de URLs → clips → post YouTube Shorts.

Usage:
    .venv\\Scripts\\python batch_pipeline.py urls.txt ^
        --out-dir output ^
        --model small ^
        --vertical 1080x1920 ^
        --karaoke ^
        --llm-enhance ^
        --groq-key "gsk_..." ^
        --youtube ^
        --min-score 7

O arquivo urls.txt deve ter uma URL por linha. Linhas com # são ignoradas.
O progresso é salvo em batch_state.json — interrompa e retome sem reprocessar.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from auto_bgm import ensure_bgm


# ─── Helpers ──────────────────────────────────────────────────────────────────

VENV_PYTHON = str(Path(__file__).parent / ".venv" / "Scripts" / "python.exe")


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def find_clips_json(out_dir: Path) -> Path | None:
    """Return the first *.clips.json found in out_dir."""
    matches = list(out_dir.glob("*.clips.json"))
    return matches[0] if matches else None


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {}


def save_state(state_file: Path, state: dict):
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Subprocesses ─────────────────────────────────────────────────────────────

def run_clipping(url: str, out_dir: Path, args, dry_run: bool) -> bool:
    """Run run_clipsai.py for a single URL. Returns True on success."""
    cmd = [
        VENV_PYTHON, "run_clipsai.py",
        "--url", url,
        "--out_dir", str(out_dir),
        "--device", args.device,
    ]
    if args.model:
        cmd += ["--model", args.model]
    if args.vertical:
        cmd += ["--vertical", args.vertical]
    if args.karaoke:
        cmd.append("--karaoke")
    if getattr(args, "karaoke_style", "yellow") != "yellow":
        cmd += ["--karaoke-style", args.karaoke_style]
    if getattr(args, "karaoke_position", "bottom") != "bottom":
        cmd += ["--karaoke-position", args.karaoke_position]
    if getattr(args, "title_card", False):
        cmd.append("--title-card")
    if args.llm_enhance:
        cmd.append("--llm-enhance")
    if args.groq_key:
        cmd += ["--groq-key", args.groq_key]
    if args.dynamic_crop:
        cmd.append("--dynamic-crop")
    if getattr(args, "bgm_dir", None):
        cmd += ["--bgm-dir", args.bgm_dir]
    if args.cookies:
        cmd += ["--cookies", args.cookies]
    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]
    if getattr(args, "cache_dir", None):
        cmd += ["--cache-dir", args.cache_dir]

    print(f"\n[CLIP] Comando:\n  {' '.join(cmd)}\n")
    if dry_run:
        return True

    result = subprocess.run(cmd, cwd=str(Path(__file__).parent))
    return result.returncode == 0


def run_posting(clips_json: Path, args, dry_run: bool) -> bool:
    """Run post_clips.py for a clips.json. Returns True on success."""
    cmd = [
        VENV_PYTHON, "post_clips.py",
        str(clips_json),
        "--min-score", str(args.min_score),
        "--yt-credentials", args.yt_credentials,
        "--yt-token", args.yt_token,
    ]
    if args.youtube:
        cmd.append("--youtube")
    if args.instagram:
        cmd.append("--instagram")
    if args.ig_app_id:
        cmd += ["--ig-app-id", args.ig_app_id]
    if args.ig_app_secret:
        cmd += ["--ig-app-secret", args.ig_app_secret]
    if args.ig_token:
        cmd += ["--ig-token", args.ig_token]

    print(f"\n[POST] Comando:\n  {' '.join(cmd)}\n")
    if dry_run:
        return True

    result = subprocess.run(cmd, cwd=str(Path(__file__).parent))
    return result.returncode == 0


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Esteira automática: processa lista de URLs e posta os clips no YouTube."
    )
    parser.add_argument("urls_file", help="Arquivo .txt com URLs (uma por linha).")
    parser.add_argument("--out-dir", default="output", help="Diretório base de saída (padrão: output).")
    parser.add_argument("--state-file", default="batch_state.json", help="Arquivo de progresso (padrão: batch_state.json).")

    # run_clipsai passthrough
    parser.add_argument("--model", default="small", help="Modelo Whisper (tiny/small/medium/large).")
    parser.add_argument("--vertical", default="1080x1920", help="Resolução vertical WxH (padrão: 1080x1920).")
    parser.add_argument("--karaoke", action="store_true", help="Legendas karaoke word-by-word.")
    parser.add_argument("--karaoke-style", default="yellow", choices=["yellow","white","red","green"], dest="karaoke_style", help="Cor do karaoke (padrão: yellow).")
    parser.add_argument("--karaoke-position", default="bottom", choices=["bottom","center","top"], dest="karaoke_position", help="Posição das legendas (padrão: bottom).")
    parser.add_argument("--title-card", action="store_true", dest="title_card", help="Exibir título animado nos primeiros 2s do clip.")
    parser.add_argument("--llm-enhance", action="store_true", dest="llm_enhance", help="Gerar título/legenda/score via Groq.")
    parser.add_argument("--groq-key", default=os.environ.get("GROQ_API_KEY", ""), dest="groq_key", help="Groq API key.")
    parser.add_argument("--dynamic-crop", action="store_true", dest="dynamic_crop", help="Crop dinâmico com face tracking.")
    parser.add_argument("--device", default="cuda", help="Device de transcrição: cuda ou cpu.")
    parser.add_argument("--cookies", default=None, help="Arquivo cookies.txt para autenticação no yt-dlp.")
    parser.add_argument("--cookies-from-browser", default=None, dest="cookies_from_browser", help="Navegador para extrair cookies (chrome, firefox, edge).")
    parser.add_argument("--cache-dir", default=None, dest="cache_dir", help="Diretório persistente de cache para vídeos baixados (padrão: output/_video_cache).")
    parser.add_argument("--bgm-dir", default=None, dest="bgm_dir", help="Pasta com .mp3/.wav royalty-free para mixar no fundo dos clips (~12%% volume).")
    parser.add_argument("--auto-bgm", action="store_true", dest="auto_bgm", help="Baixar automaticamente música de fundo adequada ao tema de cada vídeo (bensound.com).")

    # post_clips passthrough
    parser.add_argument("--youtube", action="store_true", help="Postar no YouTube Shorts após clipar.")
    parser.add_argument("--min-score", type=float, default=0, dest="min_score", help="Só posta clips com virality_score >= N.")
    parser.add_argument("--yt-credentials", default="client_secrets.json", dest="yt_credentials", help="client_secrets.json do Google.")
    parser.add_argument("--yt-token", default="youtube_token.json", dest="yt_token", help="Token OAuth YouTube.")
    parser.add_argument("--instagram", action="store_true", help="Postar no Instagram Reels após clipar.")
    parser.add_argument("--ig-app-id", default=os.environ.get("IG_APP_ID", ""), dest="ig_app_id", help="Meta App ID (ou env IG_APP_ID).")
    parser.add_argument("--ig-app-secret", default=os.environ.get("IG_APP_SECRET", ""), dest="ig_app_secret", help="Meta App Secret (ou env IG_APP_SECRET).")
    parser.add_argument("--ig-token", default="instagram_token.json", dest="ig_token", help="Arquivo de token Instagram.")

    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Mostra comandos sem executar.")

    args = parser.parse_args()

    # Validate
    urls_file = Path(args.urls_file)
    if not urls_file.exists():
        print(f"[ERRO] Arquivo de URLs não encontrado: {urls_file}")
        sys.exit(1)

    if not args.youtube and not args.instagram:
        print("[AVISO] Nenhuma plataforma de post selecionada. Use --youtube e/ou --instagram para postar.")

    # Load URLs
    raw_lines = urls_file.read_text(encoding="utf-8").splitlines()
    urls = [line.strip() for line in raw_lines if line.strip() and not line.strip().startswith("#")]

    if not urls:
        print("Nenhuma URL encontrada no arquivo.")
        sys.exit(0)

    print(f"\nURLs carregadas: {len(urls)}")
    for u in urls:
        print(f"  {u}")

    base_out = Path(args.out_dir)
    state_file = Path(args.state_file)
    state = load_state(state_file)

    results = {"ok": 0, "fail": 0, "skip": 0}

    for url in urls:
        video_id = extract_video_id(url)
        if not video_id:
            print(f"\n[AVISO] Não foi possível extrair ID de: {url} — pulando.")
            results["fail"] += 1
            continue

        print(f"\n{'=' * 60}")
        print(f"Vídeo: {video_id}  |  {url}")
        print(f"{'=' * 60}")

        entry = state.setdefault(video_id, {"url": url, "clipped": False, "posted": False, "clips_json": None, "error": None})
        out_dir = base_out / video_id

        # ── Auto BGM: download música adequada ao tema ────────────
        if getattr(args, "auto_bgm", False) and not getattr(args, "bgm_dir", None):
            bgm_dir = base_out.parent / "bgm"
            # Try to get title from cached video filename (contains title in name)
            bgm_title = entry.get("title", "")
            if not bgm_title:
                cache_dir = base_out.parent / "_video_cache"
                cached_files = list(cache_dir.glob(f"*.{video_id}.*")) if cache_dir.exists() else []
                if cached_files:
                    bgm_title = cached_files[0].stem  # filename without ext contains title
            bgm_track = ensure_bgm(bgm_dir, title=bgm_title or url)
            if bgm_track:
                args.bgm_dir = str(bgm_dir)

        # ── Stage 1: Clipagem ──────────────────────────────────────
        if entry["clipped"]:
            print(f"[CLIP] Já processado — pulando.")
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            ok = run_clipping(url, out_dir, args, args.dry_run)
            if ok:
                if not args.dry_run:
                    clips_json = find_clips_json(out_dir)
                    entry["clipped"] = True
                    entry["clips_json"] = str(clips_json) if clips_json else None
                    entry["error"] = None
                    save_state(state_file, state)
                    print(f"[CLIP] OK  →  {clips_json}")
                else:
                    clips_json = None
            else:
                entry["error"] = "run_clipsai.py falhou (exit code != 0)"
                save_state(state_file, state)
                print(f"[CLIP] FALHOU — pulando post para este vídeo.")
                results["fail"] += 1
                continue

        # ── Stage 2: Post ──────────────────────────────────────────
        if not args.youtube:
            results["ok"] += 1
            continue

        if entry["posted"]:
            print(f"[POST] Já postado — pulando.")
            results["skip"] += 1
            continue

        clips_json = Path(entry["clips_json"]) if entry.get("clips_json") else find_clips_json(out_dir)
        if not clips_json or not clips_json.exists():
            print(f"[POST] clips.json não encontrado em {out_dir} — pulando post.")
            results["fail"] += 1
            continue

        ok = run_posting(clips_json, args, args.dry_run)
        if ok:
            if not args.dry_run:
                entry["posted"] = True
                entry["error"] = None
                save_state(state_file, state)
            print(f"[POST] OK")
            results["ok"] += 1
        else:
            entry["error"] = "post_clips.py falhou (exit code != 0)"
            save_state(state_file, state)
            print(f"[POST] FALHOU")
            results["fail"] += 1

    # ── Resumo ──────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"RESUMO")
    print(f"  OK:      {results['ok']}")
    print(f"  Falhas:  {results['fail']}")
    print(f"  Pulados: {results['skip']}")
    print(f"Estado salvo em: {state_file}")
    if args.dry_run:
        print("\n[DRY-RUN] Nenhum comando foi executado de verdade.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
