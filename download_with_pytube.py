from pytube import YouTube
import sys, os

if len(sys.argv) < 2:
    print("Usage: python download_with_pytube.py <URL> [out_dir]")
    sys.exit(1)

url = sys.argv[1]
out_dir = sys.argv[2] if len(sys.argv) > 2 else "download"
os.makedirs(out_dir, exist_ok=True)

try:
    yt = YouTube(url)
    stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
    if not stream:
        print('No suitable progressive mp4 stream found', file=sys.stderr)
        sys.exit(2)
    print('Selected stream:', stream)
    out_path = stream.download(output_path=out_dir)
    print('Saved to', out_path)
except Exception as e:
    print('Error downloading:', e, file=sys.stderr)
    sys.exit(3)
