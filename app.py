from flask import Flask, request, jsonify, render_template, send_file
import os, json, uuid, requests, re, base64
from io import BytesIO
from PIL import Image
import traceback

# [추가] Google Drive API 관련 임포트
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

JOB_DIR = "jobs"
OUTPUT_DIR = "output"
os.makedirs(JOB_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ✅ 이미지 캐시
IMAGE_CACHE = {}
THUMB_MAX_SIZE = 720  # 가이드/프리뷰용 썸네일 세로 최대 크기

# [추가] Google Drive 인증 설정
SCOPES = ['https://www.googleapis.com/auth/drive.file']
# [주의] 이 파일이 app.py와 같은 위치에 있어야 합니다.
SERVICE_ACCOUNT_FILE = 'service_account.json'

def get_gdrive_service():
    """Google Drive API 서비스 객체를 생성합니다."""
    try:
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        print(f"❌ Google Drive 인증 실패: {e}")
        print(f"⚠️ '{SERVICE_ACCOUNT_FILE}' 파일이 필요하며, Google Drive API가 활성화되어 있어야 합니다.")
        print("⚠️ 또한, 대상 드라이브 폴더가 이 서비스 계정 이메일과 '공유'되어 있어야 합니다.")
        return None


def get_image_cached(url):
    """Google Drive 이미지 캐싱 (원본 + 썸네일)"""
    if url in IMAGE_CACHE:
        return IMAGE_CACHE[url]

    try:
        r = requests.get(url, timeout=10)
        img = Image.open(BytesIO(r.content)).convert("RGB")

        # 썸네일 생성
        preview = img.copy()
        preview.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE))

        IMAGE_CACHE[url] = (img, preview)
        return (img, preview)
    except Exception as e:
        print(f"❌ 이미지 캐시 실패: {url} ({e})")
        return (None, None)


# --------------------------------------------------
# 1️⃣ n8n → Flask
# --------------------------------------------------
@app.route("/job", methods=["POST"])
def receive_job():
    data = request.get_json()
    job_id = str(uuid.uuid4())

    # [수정] n8n에서 gdrive_folder_url을 받아 ID 추출
    gdrive_url = data.get("gdrive_folder_url")
    gdrive_folder_id = None
    if gdrive_url:
        gdrive_folder_id = get_gdrive_folder_id(gdrive_url)
        if gdrive_folder_id:
            data["gdrive_folder_id"] = gdrive_folder_id # job data에 ID 저장
            print(f"🎯 Google Drive 상위 폴더 ID 저장됨: {gdrive_folder_id}")
        else:
            print(f"⚠️ Google Drive URL에서 폴더 ID를 찾을 수 없습니다: {gdrive_url}")
            data["gdrive_folder_id"] = None # ID가 없음을 명시
    else:
        print("ℹ️ n8n 요청에 'gdrive_folder_url'이 없습니다.")
        data["gdrive_folder_id"] = None

    # ⭐️ [고도화] n8n 웹훅 URL 저장
    n8n_webhook_url = data.get("n8n_webhook_url")
    if n8n_webhook_url:
        data["n8n_webhook_url"] = n8n_webhook_url
        print(f"🔔 n8n 완료 알림 Webhook URL 저장됨: {n8n_webhook_url}")
    else:
        print("ℹ️ n8n 요청에 'n8n_webhook_url'이 없습니다. 완료 알림을 보내지 않습니다.")
        data["n8n_webhook_url"] = None

    job_path = os.path.join(JOB_DIR, f"{job_id}.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # [수정] IP 주소를 실제 서버 IP로 변경 (이전 수정 사항)
    gui_url = f"http://172.16.16.109:5000/gui/{job_id}" # 👈 IP 확인 필요
    return jsonify({"ok": True, "job_id": job_id, "gui_url": gui_url})


# --------------------------------------------------
# 2️⃣ GUI 페이지
# --------------------------------------------------
@app.route("/gui/<job_id>")
def gui(job_id):
    job_path = os.path.join(JOB_DIR, f"{job_id}.json")
    if not os.path.exists(job_path):
        return "❌ Job not found", 404
    with open(job_path, "r", encoding="utf-8") as f:
        job_data = json.load(f)

    return render_template("gui.html", job_id=job_id, job=job_data)


# --------------------------------------------------
# 3️⃣ 이미지 프록시
# --------------------------------------------------
@app.route("/image_proxy")
def image_proxy():
    url = request.args.get("url")
    if not url:
        return "Missing URL", 400

    try:
        _, preview = get_image_cached(url)
        if preview is None:
            return "❌ Preview fetch failed", 500

        buf = BytesIO()
        preview.save(buf, format="JPEG", quality=70)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception as e:
        return f"Proxy Error: {str(e)}", 500


# --------------------------------------------------
# 4️⃣ 유틸
# --------------------------------------------------
def merge_images_vertically(images):
    widths, heights = zip(*(i.size for i in images))
    total_height = sum(heights)
    merged = Image.new("RGB", (widths[0], total_height))
    y_offset = 0
    for img in images:
        merged.paste(img, (0, y_offset))
        y_offset += img.height
    return merged


def get_adjacent_names(base_name):
    m = re.search(r"(\d+)\.jpg$", base_name)
    if not m:
        return [base_name]
    num = int(m.group(1))
    digits = len(m.group(1))
    return [
        base_name.replace(m.group(1), str(num - 1).zfill(digits)),
        base_name,
        base_name.replace(m.group(1), str(num + 1).zfill(digits)),
    ]

def get_gdrive_folder_id(url):
    """Google Drive 폴더 URL(일반/공유 드라이브)에서 폴더 ID를 추출합니다."""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    match = re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    return None


def load_job(job_id):
    """지정된 job_id에 해당하는 job 파일을 로드합니다."""
    if not job_id or ".." in job_id or "/" in job_id or "\\" in job_id:
        print(f"❌ 유효하지 않거나 안전하지 않은 job_id 요청: {job_id}")
        return None

    job_path = os.path.join(JOB_DIR, f"{job_id}.json")
    if not os.path.exists(job_path):
        print(f"❌ Job 파일을 찾을 수 없음: {job_path}")
        return None

    try:
        with open(job_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Job 파일 로드 실패: {job_path} ({e})")
        return None

def create_gdrive_folder(service, folder_name, parent_folder_id):
    """(공유) 드라이브에 새 폴더를 만들고 ID를 반환합니다."""
    try:
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id]
        }
        folder = service.files().create(
            body=file_metadata,
            fields='id',
            supportsAllDrives=True
        ).execute()

        new_folder_id = folder.get('id')
        print(f"✅ Google Drive 폴더 생성 성공: {folder_name} (ID: {new_folder_id})")
        return new_folder_id
    except Exception as e:
        # 폴더가 이미 존재하면 오류가 발생할 수 있음 (4xx 에러)
        # 이미 존재하는 폴더를 찾아서 ID를 반환하는 로직을 추가할 수도 있음
        print(f"❌ Google Drive 폴더 생성 실패 (이름: {folder_name}, 부모: {parent_folder_id}): {e}")
        # traceback.print_exc() # 상세 에러 로그 필요 시 주석 해제
        return None

# --------------------------------------------------
# 5️⃣ Preview (썸네일 기준, 3열)
# --------------------------------------------------
@app.route("/preview_crops", methods=["POST"])
def preview_crops():
    from base64 import b64encode

    payload = request.get_json()
    job_id = payload.get("job_id")
    data = payload.get("guides", {})

    if not job_id:
        return jsonify({"ok": False, "message": "Job ID가 없습니다."}), 400

    print(f"👀 Preview Request Received: Job ID {job_id}, {len(data)} items")

    previews = {}

    job_data = load_job(job_id)
    if not job_data:
        return jsonify({"ok": False, "message": f"Job not found (ID: {job_id})."}), 400

    if "scenes" not in job_data:
        print(f"❌ Job 파일에 'scenes' 키가 없습니다. (ID: {job_id})")
        return jsonify({"ok": False, "message": "Job data is incomplete (missing 'scenes')."}), 400

    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    image_map = {img["name"]: img["url"] for img in job_data.get("all_images", [])}

    sorted_items = sorted(data.items(), key=lambda x: int(x[0].split("_")[0]))

    for key, crop in sorted_items:
        img_path = os.path.join(job_output_dir, f"{key}.jpg")

        if not os.path.exists(img_path):
            print(f"⚠️ Missing cropped file: {img_path} — generating preview...")
            try:
                scene_num, cut_type = key.split("_")

                scene_info = next(
                    (s for s in job_data["scenes"] if str(s["scene_number"]) == scene_num),
                    None,
                )
                if not scene_info:
                    print(f"⚠️ 씬 정보를 찾을 수 없음: 씬 {scene_num}")
                    continue

                base_name = (
                    scene_info["primary_cut"]
                    if "primary" in cut_type
                    else scene_info["alternative_cuts"][int(cut_type[-1]) - 1]
                )

                neighbor_names = get_adjacent_names(base_name)
                base_urls = [image_map[n] for n in neighbor_names if n in image_map]

                imgs = []
                for url in base_urls:
                    original, _ = get_image_cached(url)
                    if original:
                        imgs.append(original.copy())

                if not imgs:
                    print(f"⚠️ 원본 이미지를 찾을 수 없음 (key: {key})")
                    continue

                merged = merge_images_vertically(imgs)
                display_w, display_h = crop["display_w"], crop["display_h"]
                scale_x, scale_y = merged.width / display_w, merged.height / display_h
                x, y, w, h = (
                    int(crop["x"] * scale_x),
                    int(crop["y"] * scale_y),
                    int(crop["w"] * scale_x),
                    int(crop["h"] * scale_y),
                )
                cropped = merged.crop((x, y, x + w, y + h))

                cropped.thumbnail((1280, 1280))
                cropped.save(img_path, quality=85)

            except Exception as e:
                print(f"❌ Failed to generate preview for {key}: {e}")
                traceback.print_exc()
                continue

        try:
            with open(img_path, "rb") as f:
                encoded = b64encode(f.read()).decode("utf-8")
            previews[key] = f"data:image/jpeg;base64,{encoded}"
        except Exception as e:
            print(f"❌ Preview encode failed for {key}: {e}")

    print(f"✅ Preview returned {len(previews)} items")
    return jsonify({"ok": True, "previews": previews})


# --------------------------------------------------
# 6️⃣ Save (원본 해상도 기준)
# --------------------------------------------------
@app.route("/save_crops", methods=["POST"])
def save_crops():
    payload = request.get_json()
    job_id = payload.get("job_id")
    data = payload.get("guides", {})

    if not job_id:
        return jsonify({"ok": False, "message": "Job ID가 없습니다."}), 400

    print(f"🎯 Crop Data Received: Job ID {job_id}, {len(data)} items")

    job_data = load_job(job_id)
    if not job_data:
        return jsonify({"ok": False, "message": f"Job not found (ID: {job_id})."}), 400

    if "scenes" not in job_data:
        print(f"❌ Job 파일에 'scenes' 키가 없습니다. (ID: {job_id})")
        return jsonify({"ok": False, "message": "Job data is incomplete (missing 'scenes')."}), 400

    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    image_map = {img["name"]: img["url"] for img in job_data.get("all_images", [])}

    parent_gdrive_folder_id = job_data.get("gdrive_folder_id")
    service = None
    target_upload_folder_id = None # 최종 업로드될 GDrive 폴더 ID

    if parent_gdrive_folder_id:
        print(f"🚀 Google Drive 업로드를 준비합니다. (상위 폴더 ID: {parent_gdrive_folder_id})")
        service = get_gdrive_service()

        if service:
            folder_name_to_create = job_id
            new_folder_id = create_gdrive_folder(service, folder_name_to_create, parent_gdrive_folder_id)

            if new_folder_id:
                target_upload_folder_id = new_folder_id
            else:
                print(f"⚠️ {job_id} 서브폴더 생성 실패. 상위 폴더({parent_gdrive_folder_id})에 업로드합니다.")
                target_upload_folder_id = parent_gdrive_folder_id
        else:
            print("⚠️ Google Drive 서비스 인증 실패, 로컬에만 저장합니다.")
    else:
        print("ℹ️ Google Drive 폴더 ID가 job에 없습니다. 로컬에만 저장합니다.")

    upload_count = 0
    processed_files = [] # ⭐️ [고도화] 업로드된 파일 정보 저장용 리스트

    for key, crop in data.items():
        if crop["w"] <= 1 or crop["h"] <= 1:
            print(f"ℹ️ 크기가 1보다 작은 crop 데이터는 건너뜁니다: {key}")
            continue
        try:
            scene_num, cut_type = key.split("_")
        except ValueError:
            print(f"⚠️ 잘못된 key 형식입니다: {key}")
            continue

        scene_info = next(
            (s for s in job_data["scenes"] if str(s["scene_number"]) == scene_num),
            None,
        )
        if not scene_info:
            print(f"⚠️ 씬 정보를 찾을 수 없음: 씬 {scene_num}")
            continue

        try:
            base_name = (
                scene_info["primary_cut"]
                if "primary" in cut_type
                else scene_info["alternative_cuts"][int(cut_type[-1]) - 1]
            )
        except Exception as e:
            print(f"❌ 씬 정보에서 base_name을 찾는 중 오류 (key: {key}): {e}")
            continue

        neighbor_names = get_adjacent_names(base_name)
        base_urls = [image_map[n] for n in neighbor_names if n in image_map]
        imgs = []

        for url in base_urls:
            original, _ = get_image_cached(url)
            if original:
                imgs.append(original.copy())
        if not imgs:
            print(f"⚠️ 원본 이미지를 찾을 수 없음 (key: {key}, base_name: {base_name})")
            continue

        merged = merge_images_vertically(imgs)

        display_w, display_h = crop["display_w"], crop["display_h"]
        scale_x, scale_y = merged.width / display_w, merged.height / display_h
        x, y, w, h = (
            int(crop["x"] * scale_x),
            int(crop["y"] * scale_y),
            int(crop["w"] * scale_x),
            int(crop["h"] * scale_y),
        )

        try:
            cropped = merged.crop((x, y, x + w, y + h))
            out_path = os.path.join(job_output_dir, f"{key}.jpg")

            # 1. 로컬에 저장
            cropped.save(out_path, quality=90)
            print(f"✅ Saved merged crop locally: {out_path}")

            # 2. Google Drive에 업로드
            if service and target_upload_folder_id:
                try:
                    file_name = f"{key}.jpg"
                    img_buffer = BytesIO()
                    cropped.save(img_buffer, format="JPEG", quality=90)
                    img_buffer.seek(0)

                    file_metadata = {
                        'name': file_name,
                        'parents': [target_upload_folder_id]
                    }

                    media = MediaIoBaseUpload(
                        img_buffer,
                        mimetype='image/jpeg',
                        resumable=True
                    )

                    file = service.files().create(
                        body=file_metadata,
                        media_body=media,
                        fields='id, webViewLink', # ⭐️ [고도화] 웹 링크도 함께 받아오기
                        supportsAllDrives=True
                    ).execute()

                    print(f"🚀 Google Drive 업로드 성공: {file_name} (File ID: {file.get('id')})")
                    upload_count += 1
                    # ⭐️ [고도화] 업로드된 파일 정보 추가 (n8n으로 보낼 데이터)
                    processed_files.append({
                        "key": key,
                        "file_name": file_name,
                        "gdrive_id": file.get('id'),
                        "gdrive_link": file.get('webViewLink')
                    })

                except Exception as e:
                    print(f"❌ Google Drive 업로드 실패 ({key}): {e}")
                    # traceback.print_exc() # 상세 로그 필요 시 주석 해제

        except Exception as e:
            print(f"❌ 크롭 이미지 저장/업로드 실패 (key: {key}): {e}")
            traceback.print_exc()

    # --- 모든 파일 처리 루프 종료 ---

    # ⭐️ [고도화] n8n으로 완료 알림 보내기
    n8n_webhook_url = job_data.get("n8n_webhook_url")
    if n8n_webhook_url:
        print(f"🔔 n8n으로 작업 완료 알림을 보냅니다... (URL: {n8n_webhook_url})")
        try:
            # n8n으로 보낼 데이터 구성
            webhook_payload = {
                "job_id": job_id,
                "status": "completed",
                "total_guides": len(data),
                "uploaded_files_count": upload_count,
                "gdrive_target_folder_id": target_upload_folder_id,
                "processed_files": processed_files # 개별 파일 정보 리스트
            }
            # POST 요청 보내기 (타임아웃 설정)
            response = requests.post(n8n_webhook_url, json=webhook_payload, timeout=10)
            response.raise_for_status() # HTTP 오류 발생 시 예외 발생
            print(f"✅ n8n 알림 전송 성공! (상태 코드: {response.status_code})")
        except requests.exceptions.RequestException as e:
            print(f"❌ n8n 알림 전송 실패: {e}")
        except Exception as e:
            print(f"❌ n8n 알림 처리 중 예상치 못한 오류: {e}")
            traceback.print_exc()
    else:
        print("ℹ️ n8n Webhook URL이 없어 알림을 보내지 않았습니다.")

    # GUI에 보낼 최종 응답 메시지
    message = f"Merged crops saved locally! (Total {len(data)} items processed)"
    if service and target_upload_folder_id:
        message += f" | Google Drive uploads: {upload_count} successful."
        if target_upload_folder_id != parent_gdrive_folder_id:
             message += f" (Folder: {job_id})"

    return jsonify({"ok": True, "message": message})


# --------------------------------------------------
# 7️⃣ 실행
# --------------------------------------------------
if __name__ == "__main__":
    print("📜 Registered routes:")
    for rule in app.url_map.iter_rules():
        print(f" - {rule.endpoint} ({rule.methods}) -> {rule}")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)