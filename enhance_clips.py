"""
enhance_clips.py — Roda somente o Groq LLM em clips.json já existentes.

Usage:
    .venv\\Scripts\\python enhance_clips.py output/IIs5XF6b6Wo --groq-key "gsk_..."

    # Todas as 3 pastas de uma vez:
    .venv\\Scripts\\python enhance_clips.py output/IIs5XF6b6Wo output/QBtL74oCM1Y --groq-key "gsk_..."
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path


def load_word_info(trans_json_path: Path) -> list:
    with open(trans_json_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("_word_info") or []
    return []


def extract_words_for_clip(word_info: list, start: float, end: float) -> list:
    words = []
    for w in word_info:
        wt = w.get("start_time")
        if wt is None:
            continue
        wt = float(wt)
        if start <= wt <= end:
            words.append({"word": w.get("word", "")})
    return words


def analyze_clip_with_groq(clip_text: str, duration_sec: float, api_key: str) -> dict | None:
    try:
        from groq import Groq
    except ImportError:
        print("[ERRO] pip install groq")
        sys.exit(1)

    client = Groq(api_key=api_key)
    prompt = (
        "Analise este trecho de vídeo para YouTube Shorts/TikTok em Português:\n\n"
        f"Duração: {duration_sec:.0f} segundos\n"
        f"Transcrição: {clip_text[:1500]}\n\n"
        "Responda APENAS com JSON válido nesta estrutura (sem markdown):\n"
        '{"titulo": "título curto e chamativo em português (máx 60 chars)", '
        '"legenda": "legenda com emojis e hashtags em português (máx 220 chars)", '
        '"score_viralidade": <número inteiro 1-10>, '
        '"motivo": "1 frase explicando o potencial viral"}'
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip()
        # strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        print(f"    [Groq ERRO] {e}")
        return None


def process_folder(folder: Path, groq_key: str, overwrite: bool):
    clips_files = list(folder.glob("*.clips.json"))
    if not clips_files:
        print(f"[AVISO] Nenhum clips.json em {folder}")
        return

    clips_json_path = clips_files[0]
    clips = json.loads(clips_json_path.read_text(encoding="utf-8"))

    # Check if already enhanced
    already_done = [c for c in clips if c.get("title")]
    if already_done and not overwrite:
        print(f"[{folder.name}] Já tem {len(already_done)}/{len(clips)} clips com título. Use --overwrite para refazer.")
        return

    # Load transcription for text extraction
    trans_files = list(folder.glob("*.transcription.json"))
    word_info = []
    if trans_files:
        word_info = load_word_info(trans_files[0])
        print(f"[{folder.name}] {len(word_info)} palavras carregadas da transcrição")
    else:
        print(f"[{folder.name}] Sem transcription.json — texto vazio será usado")

    print(f"[{folder.name}] Analisando {len(clips)} clips com Groq...")
    for clip in clips:
        if clip.get("title") and not overwrite:
            continue
        i = clip["index"]
        clip_words = extract_words_for_clip(word_info, clip["start_time"], clip["end_time"])
        clip_text = " ".join(w["word"] for w in clip_words)
        duration = clip["end_time"] - clip["start_time"]
        print(f"  Clip {i:03d} ({duration:.0f}s)...", end=" ", flush=True)
        result = analyze_clip_with_groq(clip_text, duration, groq_key)
        if result:
            clip["title"] = result.get("titulo", "")
            clip["caption"] = result.get("legenda", "")
            clip["virality_score"] = result.get("score_viralidade", 0)
            clip["virality_reason"] = result.get("motivo", "")
            print(f"[{clip['virality_score']}] {clip['title']}")
        else:
            print("falhou")

        # Save after each clip in case of interruption
        clips_json_path.write_text(
            json.dumps(clips, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(f"[{folder.name}] Salvo: {clips_json_path.name}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Adiciona título, legenda e score de viralidade via Groq em clips.json existentes."
    )
    parser.add_argument("folders", nargs="+", help="Pastas com clips.json (ex: output/IIs5XF6b6Wo)")
    parser.add_argument("--groq-key", default=os.environ.get("GROQ_API_KEY", ""), dest="groq_key")
    parser.add_argument("--overwrite", action="store_true", help="Reanalisar clips que já têm título")
    args = parser.parse_args()

    if not args.groq_key:
        print("[ERRO] Forneça a chave Groq com --groq-key ou env GROQ_API_KEY")
        sys.exit(1)

    for folder_str in args.folders:
        folder = Path(folder_str)
        if not folder.exists():
            print(f"[AVISO] Pasta não encontrada: {folder}")
            continue
        process_folder(folder, args.groq_key, args.overwrite)

    print("Concluído. Agora rode post_clips.py para postar no YouTube:")
    for folder_str in args.folders:
        folder = Path(folder_str)
        clips_files = list(folder.glob("*.clips.json"))
        if clips_files:
            print(f"  .venv\\Scripts\\python post_clips.py \"{clips_files[0]}\" --youtube --min-score 7")


if __name__ == "__main__":
    main()
