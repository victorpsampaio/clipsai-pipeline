"""
auto_bgm.py — Download automático de música de fundo royalty-free baseado no tema do vídeo.

Fontes (em ordem de tentativa):
  1. Jamendo API (CC-licensed, gratuita, sem chave própria)
  2. ccMixter (Creative Commons)

Uso:
    from auto_bgm import ensure_bgm
    bgm_path = ensure_bgm(bgm_dir=Path("bgm"), title="URGENTE! RONALDO EXPLICA...")
"""

import json
import urllib.request
import urllib.error
from pathlib import Path

# ─── Tags Jamendo por categoria ───────────────────────────────────────────────
# API pública: https://developer.jamendo.com/v3.0/tracks
# client_id público para uso em apps open source

_JAMENDO_CLIENT_ID = "b6747d04"

_JAMENDO_TAGS: dict[str, str] = {
    "sports":        "energetic+upbeat",
    "news":          "dramatic+inspiring",
    "entertainment": "happy+fun",
    "default":       "background+calm",
}

_BGM_FILENAME: dict[str, str] = {
    "sports":        "bgm_sports.mp3",
    "news":          "bgm_news.mp3",
    "entertainment": "bgm_entertainment.mp3",
    "default":       "bgm_default.mp3",
}

# ─── Keyword → category ───────────────────────────────────────────────────────

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "sports": [
        "futebol", "ronaldo", "neymar", "messi", "copa", "gol", "jogo",
        "time", "campeão", "bola", "jogador", "clube", "seleção", "brasileirão",
        "soccer", "football", "nba", "nfl", "esporte", "sport", "atleta",
        "olimpíadas", "champions", "libertadores", "vôlei", "basquete",
    ],
    "news": [
        "urgente", "notícia", "noticia", "política", "politica", "governo",
        "presidente", "eleição", "eleicao", "crise", "guerra", "ataque",
        "news", "breaking", "exclusivo", "revelou", "confirmado", "suspeito",
        "economia", "bolsonaro", "lula", "congresso", "stf", "policia",
    ],
    "entertainment": [
        "reação", "reacao", "react", "viral", "incrível", "incrivel",
        "parabéns", "festa", "comedy", "comédia", "comedia", "meme",
        "influencer", "tiktok", "trend", "challenge", "dança", "danca",
        "música", "musica", "show", "celebridade", "famoso",
    ],
}


def detect_category(title: str) -> str:
    """Detect video category from title using keyword matching."""
    if not title:
        return "default"
    title_lower = title.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return category
    return "default"


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _http_get(url: str, timeout: int = 30) -> bytes | None:
    """GET a URL and return bytes, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _download_file(url: str, dest: Path, min_size: int = 50_000) -> bool:
    """Download URL to dest. Returns True on success."""
    data = _http_get(url)
    if not data or len(data) < min_size:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def _jamendo_download(category: str, dest: Path) -> bool:
    """Search Jamendo for a CC-licensed track and download it."""
    tags = _JAMENDO_TAGS.get(category, _JAMENDO_TAGS["default"])
    api_url = (
        f"https://api.jamendo.com/v3.0/tracks/"
        f"?client_id={_JAMENDO_CLIENT_ID}&format=json&limit=5"
        f"&tags={tags}&audioformat=mp32&order=popularity_total"
    )
    raw = _http_get(api_url, timeout=15)
    if not raw:
        return False
    try:
        results = json.loads(raw).get("results", [])
    except Exception:
        return False
    for track in results:
        audio_url = track.get("audio") or track.get("audiodownload")
        if not audio_url:
            continue
        name = track.get("name", "unknown")
        artist = track.get("artist_name", "unknown")
        print(f"[BGM]   Jamendo: '{name}' by {artist}")
        if _download_file(audio_url, dest):
            print(f"[BGM]   License: CC (jamendo.com) — {track.get('license_ccurl', '')}")
            return True
    return False


def ensure_bgm(bgm_dir: Path, title: str = "") -> "Path | None":
    """Ensure a BGM track appropriate for the video title exists in bgm_dir.

    Downloads the track if not already cached. Returns the track path, or None
    if all sources fail.
    """
    bgm_dir.mkdir(parents=True, exist_ok=True)
    category = detect_category(title)
    dest = bgm_dir / _BGM_FILENAME.get(category, "bgm_default.mp3")

    if dest.exists() and dest.stat().st_size > 50_000:
        print(f"[BGM] Cache: {dest.name} (categoria: {category})")
        return dest

    print(f"[BGM] Categoria: {category} — baixando música de fundo via Jamendo...")
    if _jamendo_download(category, dest):
        print(f"[BGM] OK → {dest.name} ({dest.stat().st_size // 1024}KB)")
        return dest

    # Fallback: try default if different category failed
    if category != "default":
        dest_fallback = bgm_dir / _BGM_FILENAME["default"]
        if dest_fallback.exists() and dest_fallback.stat().st_size > 50_000:
            print(f"[BGM] Usando fallback: {dest_fallback.name}")
            return dest_fallback
        print("[BGM] Tentando categoria default...")
        if _jamendo_download("default", dest_fallback):
            print(f"[BGM] Fallback OK → {dest_fallback.name}")
            return dest_fallback

    print("[BGM] Não foi possível baixar música de fundo — continuando sem BGM.")
    return None


if __name__ == "__main__":
    import sys
    title = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    result = ensure_bgm(Path("bgm"), title=title)
    print(f"BGM: {result}")
