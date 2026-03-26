"""
post_clips.py — Auto-post clips to YouTube Shorts, TikTok and Instagram Reels.

Usage:
    python post_clips.py ./output/video.clips.json --youtube --tiktok \
        --yt-credentials client_secrets.json \
        --tiktok-key "CLIENT_KEY" --tiktok-secret "CLIENT_SECRET" \
        --min-score 7

    python post_clips.py ./output/video.clips.json --instagram \
        --ig-app-id "APP_ID" --ig-app-secret "APP_SECRET" \
        --min-score 7

Requirements:
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 requests
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─── YouTube ──────────────────────────────────────────────────────────────────

def get_youtube_service(credentials_file: str, token_file: str):
    """Load saved OAuth token or run browser flow. Returns YouTube API service."""
    try:
        from googleapiclient.discovery import build
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        import google.auth.exceptions
    except ImportError:
        print(
            "[ERRO] Pacotes do Google não instalados.\n"
            "Execute: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )
        sys.exit(1)

    SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = None

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except google.auth.exceptions.RefreshError:
                creds = None

        if not creds:
            if not Path(credentials_file).exists():
                print(
                    f"[ERRO] Arquivo de credenciais '{credentials_file}' não encontrado.\n"
                    "Acesse console.cloud.google.com → APIs → YouTube Data API v3 → Credenciais → OAuth 2.0 Desktop"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as f:
            f.write(creds.to_json())
        print(f"[YouTube] Token salvo em {token_file}")

    return build("youtube", "v3", credentials=creds)


def _extract_hashtags(text: str) -> list[str]:
    """Extract hashtag words from caption text."""
    return [tag.lstrip("#") for tag in re.findall(r"#\w+", text)]


def post_to_youtube(service, clip: dict) -> str:
    """Upload a clip to YouTube Shorts. Returns the video URL."""
    from googleapiclient.http import MediaFileUpload
    import googleapiclient.errors

    video_path = clip["file"]
    title = clip.get("title", "Clip")[:100]  # YouTube max title length
    caption = clip.get("caption", "")

    # Build hashtag block: LLM hashtags + fixed viral tags (max 8 total)
    llm_hashtags = clip.get("hashtags", [])
    fixed_hashtags = ["#Shorts", "#Viral", "#Trending"]
    all_hashtags = list(llm_hashtags) + [h for h in fixed_hashtags if h not in llm_hashtags]
    all_hashtags = all_hashtags[:8]
    hashtag_block = " ".join(all_hashtags)
    description = f"{caption}\n\n{hashtag_block}"
    tags = [h.lstrip("#") for h in all_hashtags] + ["Shorts"]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "24",  # Entertainment
        },
        "status": {
            "privacyStatus": "public",
            "madeForKids": False,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=256 * 1024, resumable=True, mimetype="video/*")

    print(f"  [YouTube] Enviando: {Path(video_path).name}")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"  [YouTube] Upload: {pct}%", end="\r")

    video_id = response["id"]
    url = f"https://www.youtube.com/shorts/{video_id}"
    print(f"  [YouTube] Publicado: {url}        ")

    # Upload thumbnail if available (clip{i:03d}.jpg next to the video)
    thumb_path = clip.get("thumbnail")
    if thumb_path and Path(thumb_path).exists():
        try:
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
            ).execute()
            print(f"  [YouTube] Thumbnail enviada: {Path(thumb_path).name}")
        except Exception as e:
            print(f"  [YouTube] Thumbnail falhou: {e}")

    return url


# ─── TikTok ───────────────────────────────────────────────────────────────────

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler to capture the OAuth callback code."""
    code = None

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        _OAuthCallbackHandler.code = qs.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<h2>Autorizado! Pode fechar esta aba.</h2>"
        )

    def log_message(self, *args):
        pass  # suppress request logs


def get_tiktok_token(client_key: str, client_secret: str, token_file: str) -> dict:
    """
    OAuth 2.0 flow for TikTok Content Posting API.
    On first run opens browser; saves token dict to token_file.
    Returns token dict with 'access_token' key.
    """
    import requests

    token_path = Path(token_file)
    if token_path.exists():
        token_data = json.loads(token_path.read_text())
        # Check expiry with 5-min buffer
        if token_data.get("expires_at", 0) > time.time() + 300:
            print(f"[TikTok] Token carregado de {token_file}")
            return token_data
        # Try refresh
        if token_data.get("refresh_token"):
            resp = requests.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key": client_key,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": token_data["refresh_token"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if resp.ok:
                new_data = resp.json()
                new_data["expires_at"] = time.time() + new_data.get("expires_in", 86400)
                token_path.write_text(json.dumps(new_data, indent=2))
                print("[TikTok] Token renovado.")
                return new_data

    REDIRECT_URI = "http://localhost:8080"
    SCOPES = "video.publish,video.upload"

    # PKCE: code_verifier + code_challenge (S256)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    auth_url = (
        "https://www.tiktok.com/v2/auth/authorize/?"
        + urlencode({
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": "tiktok_auth",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
    )

    print(f"[TikTok] Abrindo navegador para autorização...\n{auth_url}")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), _OAuthCallbackHandler)
    server.handle_request()  # Wait for a single callback

    code = _OAuthCallbackHandler.code
    if not code:
        print("[ERRO] Não foi possível obter o código de autorização TikTok.")
        sys.exit(1)

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    token_data = resp.json()
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 86400)
    token_path.write_text(json.dumps(token_data, indent=2))
    print(f"[TikTok] Token salvo em {token_file}")
    return token_data


def post_to_tiktok(token_data: dict, clip: dict) -> str:
    """
    Upload a clip to TikTok via Content Posting API v2.
    Returns the TikTok post URL.
    """
    import requests

    access_token = token_data["access_token"]
    video_path = Path(clip["file"])
    title = clip.get("title", "")[:150]  # TikTok title limit

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    # Step 1: Initialize upload
    video_size = video_path.stat().st_size
    init_payload = {
        "post_info": {
            "title": title,
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": video_size,
            "total_chunk_count": 1,
        },
    }

    print(f"  [TikTok] Inicializando upload: {video_path.name}")
    resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers=headers,
        json=init_payload,
        timeout=30,
    )
    resp.raise_for_status()
    init_data = resp.json()

    if init_data.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok init error: {init_data['error']}")

    publish_id = init_data["data"]["publish_id"]
    upload_url = init_data["data"]["upload_url"]

    # Step 2: Upload video binary
    print(f"  [TikTok] Enviando vídeo...")
    with open(video_path, "rb") as fh:
        video_bytes = fh.read()

    upload_headers = {
        "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
        "Content-Type": "video/mp4",
        "Content-Length": str(video_size),
    }
    up_resp = requests.put(upload_url, data=video_bytes, headers=upload_headers, timeout=300)
    up_resp.raise_for_status()

    # Step 3: Poll for publish status
    print(f"  [TikTok] Aguardando publicação (publish_id={publish_id})...")
    for attempt in range(30):
        time.sleep(5)
        status_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers=headers,
            json={"publish_id": publish_id},
            timeout=30,
        )
        status_resp.raise_for_status()
        status_data = status_resp.json()
        status = status_data.get("data", {}).get("status", "")
        print(f"  [TikTok] Status: {status} ({attempt + 1}/30)", end="\r")

        if status == "PUBLISH_COMPLETE":
            share_url = status_data["data"].get("share_url", "")
            print(f"\n  [TikTok] Publicado: {share_url}")
            return share_url
        elif status in ("FAILED", "CANCELLED"):
            fail_reason = status_data["data"].get("fail_reason", "unknown")
            raise RuntimeError(f"TikTok publicação falhou: {fail_reason}")

    raise TimeoutError("TikTok: timeout aguardando publicação (150s).")


# ─── Instagram ────────────────────────────────────────────────────────────────

GRAPH_API = "https://graph.facebook.com/v20.0"
REDIRECT_URI = "http://localhost:8080"


def get_instagram_token(app_id: str, app_secret: str, token_file: str) -> dict:
    """
    OAuth 2.0 via Facebook Login para Instagram Content Publishing API.
    Na primeira execução abre o browser; salva token em token_file.
    Tokens duram ~60 dias.
    """
    import requests

    token_path = Path(token_file)
    if token_path.exists():
        data = json.loads(token_path.read_text())
        if data.get("expires_at", 0) > time.time() + 3600:
            print(f"[Instagram] Token carregado de {token_file}")
            return data

    # ── OAuth flow ──
    auth_url = (
        "https://www.facebook.com/v20.0/dialog/oauth?"
        + urlencode({
            "client_id": app_id,
            "redirect_uri": REDIRECT_URI,
            "scope": "instagram_basic,instagram_content_publish",
            "response_type": "code",
        })
    )
    print(f"[Instagram] Abrindo navegador para autorização...")
    webbrowser.open(auth_url)

    # Capturar code via servidor local
    _OAuthCallbackHandler.code = None
    server = HTTPServer(("localhost", 8080), _OAuthCallbackHandler)
    server.handle_request()
    code = _OAuthCallbackHandler.code
    if not code:
        raise RuntimeError("[Instagram] Autorização cancelada — code não recebido.")

    # Trocar code por short-lived token
    resp = requests.post(
        f"{GRAPH_API}/oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )
    resp.raise_for_status()
    short_token = resp.json()["access_token"]

    # Trocar por long-lived token (~60 dias)
    resp = requests.get(
        f"{GRAPH_API}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    resp.raise_for_status()
    ll = resp.json()
    access_token = ll["access_token"]
    expires_in = ll.get("expires_in", 5184000)  # ~60 dias default

    # Obter ig_user_id via Facebook Pages
    resp = requests.get(
        f"{GRAPH_API}/me/accounts",
        params={"access_token": access_token},
    )
    resp.raise_for_status()
    pages = resp.json().get("data", [])
    ig_user_id = None
    for page in pages:
        page_token = page.get("access_token", access_token)
        r2 = requests.get(
            f"{GRAPH_API}/{page['id']}",
            params={"fields": "instagram_business_account", "access_token": page_token},
        )
        ig = r2.json().get("instagram_business_account", {})
        if ig.get("id"):
            ig_user_id = ig["id"]
            access_token = page_token  # usar page token para posts
            break

    if not ig_user_id:
        raise RuntimeError(
            "[Instagram] Conta Instagram Business/Creator não encontrada. "
            "A conta deve ser Business ou Creator e estar vinculada a uma Página do Facebook."
        )

    token_data = {
        "access_token": access_token,
        "ig_user_id": ig_user_id,
        "expires_at": time.time() + expires_in,
    }
    token_path.write_text(json.dumps(token_data, indent=2))
    print(f"[Instagram] Token salvo em {token_file} (ig_user_id={ig_user_id})")
    return token_data


def post_to_instagram(token_data: dict, clip: dict) -> str:
    """
    Publica um Reel no Instagram via Meta Content Publishing API.
    Retorna a URL do post publicado.
    """
    import requests

    access_token = token_data["access_token"]
    ig_user_id = token_data["ig_user_id"]
    video_path = clip["file"]
    file_size = Path(video_path).stat().st_size
    caption = clip.get("caption", clip.get("title", ""))[:2200]

    print(f"  [Instagram] Iniciando upload: {Path(video_path).name}")

    # Passo 1: criar container resumível
    resp = requests.post(
        f"{GRAPH_API}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    container_id = result.get("id")
    upload_uri = result.get("uri")
    if not container_id or not upload_uri:
        raise RuntimeError(f"[Instagram] Resposta inesperada ao criar container: {result}")

    # Passo 2: upload do arquivo
    with open(video_path, "rb") as f:
        video_bytes = f.read()

    up_resp = requests.post(
        upload_uri,
        headers={
            "Authorization": f"OAuth {access_token}",
            "offset": "0",
            "file_size": str(file_size),
        },
        data=video_bytes,
        timeout=300,
    )
    up_resp.raise_for_status()

    # Passo 3: aguardar processamento do container
    print(f"  [Instagram] Aguardando processamento...", end="", flush=True)
    for _ in range(12):
        time.sleep(5)
        status_resp = requests.get(
            f"{GRAPH_API}/{container_id}",
            params={"fields": "status_code,status", "access_token": access_token},
        )
        status_resp.raise_for_status()
        s = status_resp.json()
        code = s.get("status_code", "")
        print(".", end="", flush=True)
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"[Instagram] Container falhou: {s.get('status')}")
    else:
        raise TimeoutError("[Instagram] Timeout aguardando processamento do container.")
    print()

    # Passo 4: publicar
    pub_resp = requests.post(
        f"{GRAPH_API}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": access_token},
    )
    pub_resp.raise_for_status()
    post_id = pub_resp.json().get("id", "")
    url = f"https://www.instagram.com/p/{post_id}/"
    print(f"  [Instagram] Publicado: {url}")
    return url


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Posta clips no YouTube Shorts e/ou TikTok a partir de um clips.json."
    )
    parser.add_argument("clips_json", help="Caminho para o arquivo clips.json gerado pelo pipeline.")
    parser.add_argument("--youtube", action="store_true", help="Postar no YouTube Shorts.")
    parser.add_argument("--tiktok", action="store_true", help="Postar no TikTok.")
    parser.add_argument(
        "--yt-credentials",
        default="client_secrets.json",
        metavar="FILE",
        help="Arquivo client_secrets.json do Google (padrão: client_secrets.json).",
    )
    parser.add_argument(
        "--yt-token",
        default="youtube_token.json",
        metavar="FILE",
        help="Arquivo para salvar/carregar token OAuth do YouTube (padrão: youtube_token.json).",
    )
    parser.add_argument(
        "--tiktok-key",
        default=os.environ.get("TIKTOK_CLIENT_KEY", ""),
        metavar="KEY",
        help="TikTok client_key (ou env TIKTOK_CLIENT_KEY).",
    )
    parser.add_argument(
        "--tiktok-secret",
        default=os.environ.get("TIKTOK_CLIENT_SECRET", ""),
        metavar="SECRET",
        help="TikTok client_secret (ou env TIKTOK_CLIENT_SECRET).",
    )
    parser.add_argument(
        "--tiktok-token",
        default="tiktok_token.json",
        metavar="FILE",
        help="Arquivo para salvar/carregar token TikTok (padrão: tiktok_token.json).",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0,
        metavar="N",
        help="Só posta clips com virality_score >= N (padrão: 0 = todos).",
    )
    parser.add_argument("--instagram", action="store_true", help="Postar no Instagram Reels.")
    parser.add_argument(
        "--ig-app-id",
        default=os.environ.get("IG_APP_ID", ""),
        metavar="ID",
        help="Meta App ID (ou env IG_APP_ID).",
    )
    parser.add_argument(
        "--ig-app-secret",
        default=os.environ.get("IG_APP_SECRET", ""),
        metavar="SECRET",
        help="Meta App Secret (ou env IG_APP_SECRET).",
    )
    parser.add_argument(
        "--ig-token",
        default="instagram_token.json",
        metavar="FILE",
        help="Arquivo para salvar/carregar token Instagram (padrão: instagram_token.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o que seria postado sem postar de verdade.",
    )
    args = parser.parse_args()

    if not args.youtube and not args.tiktok and not args.instagram:
        parser.error("Especifique pelo menos --youtube, --tiktok ou --instagram.")

    # Load clips.json
    clips_json_path = Path(args.clips_json)
    if not clips_json_path.exists():
        print(f"[ERRO] Arquivo não encontrado: {clips_json_path}")
        sys.exit(1)

    clips = json.loads(clips_json_path.read_text(encoding="utf-8"))

    # Filter by min score
    if args.min_score > 0:
        before = len(clips)
        clips = [c for c in clips if c.get("virality_score", 0) >= args.min_score]
        print(f"Filtro --min-score {args.min_score}: {len(clips)}/{before} clips selecionados.")

    if not clips:
        print("Nenhum clip para postar.")
        return

    print(f"\n{'=' * 60}")
    print(f"Clips a postar: {len(clips)}")
    for c in clips:
        score = c.get("virality_score", "?")
        print(f"  [{score}] {c.get('title', 'sem título')} — {Path(c['file']).name}")
    print(f"{'=' * 60}\n")

    if args.dry_run:
        print("[DRY-RUN] Nenhum post foi enviado.")
        return

    # Initialize services
    yt_service = None
    tiktok_token = None
    instagram_token = None

    if args.youtube:
        print("[YouTube] Autenticando...")
        yt_service = get_youtube_service(args.yt_credentials, args.yt_token)
        print("[YouTube] Pronto.\n")

    if args.tiktok:
        if not args.tiktok_key or not args.tiktok_secret:
            print(
                "[ERRO] --tiktok-key e --tiktok-secret são obrigatórios para postar no TikTok.\n"
                "Ou defina as variáveis TIKTOK_CLIENT_KEY e TIKTOK_CLIENT_SECRET."
            )
            sys.exit(1)
        print("[TikTok] Autenticando...")
        tiktok_token = get_tiktok_token(args.tiktok_key, args.tiktok_secret, args.tiktok_token)
        print("[TikTok] Pronto.\n")

    if args.instagram:
        if not args.ig_app_id or not args.ig_app_secret:
            print(
                "[ERRO] --ig-app-id e --ig-app-secret são obrigatórios para postar no Instagram.\n"
                "Ou defina as variáveis IG_APP_ID e IG_APP_SECRET."
            )
            sys.exit(1)
        print("[Instagram] Autenticando...")
        instagram_token = get_instagram_token(args.ig_app_id, args.ig_app_secret, args.ig_token)
        print("[Instagram] Pronto.\n")

    # Load full clips list for updating JSON (may include filtered-out clips)
    all_clips = json.loads(clips_json_path.read_text(encoding="utf-8"))
    clips_by_index = {c["index"]: c for c in all_clips}

    # Post each clip
    for clip in clips:
        idx = clip["index"]
        title = clip.get("title", f"clip{idx:03d}")
        print(f"\n[{idx}] {title}")

        video_file = Path(clip["file"])
        if not video_file.exists():
            print(f"  [AVISO] Arquivo não encontrado: {video_file} — pulando.")
            continue

        if yt_service:
            try:
                yt_url = post_to_youtube(yt_service, clip)
                clips_by_index[idx]["youtube_url"] = yt_url
                time.sleep(3)  # delay entre uploads
            except Exception as e:
                print(f"  [YouTube ERRO] {e}")
                err_str = str(e)
                if "uploadLimitExceeded" in err_str:
                    print("  [YouTube] Limite de uploads atingido. Verifique sua conta em youtube.com/verify")
                    print("  [YouTube] Interrompendo — retome amanhã quando o limite resetar.")
                    break

        if tiktok_token:
            try:
                tt_url = post_to_tiktok(tiktok_token, clip)
                clips_by_index[idx]["tiktok_url"] = tt_url
            except Exception as e:
                print(f"  [TikTok ERRO] {e}")

        if instagram_token:
            try:
                ig_url = post_to_instagram(instagram_token, clip)
                clips_by_index[idx]["instagram_url"] = ig_url
                time.sleep(3)
            except Exception as e:
                print(f"  [Instagram ERRO] {e}")

        # Save progress after each clip
        clips_json_path.write_text(
            json.dumps(list(clips_by_index.values()), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"\n[✓] Concluído. clips.json atualizado com as URLs.")


if __name__ == "__main__":
    main()
