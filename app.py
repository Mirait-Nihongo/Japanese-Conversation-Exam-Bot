import streamlit as st
import os
import time
import tempfile
import datetime
import vertexai
from vertexai.generative_models import GenerativeModel, SafetySetting
from google.cloud import speech
from google.cloud import texttospeech
from google.oauth2 import service_account
import gspread

# --- ページ設定 ---
st.set_page_config(page_title="日本語会話試験システム (Vertex AI)", page_icon="☁️", layout="wide")

# --- 定数・初期設定 ---
MATERIALS_DIR = "materials"
OPI_PHASES = {
    "warmup": "導入 (Warm-up)",
    "level_check": "レベルチェック",
    "probe": "突き上げ (Probe)",
    "wind_down": "終結 (Wind-down)"
}
PHASE_ORDER = ["warmup", "level_check", "level_check", "probe", "wind_down"]

# 管理者パスワード
ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", "admin")

# --- 認証関係 (Vertex AI & Google Cloud) ---
def get_gcp_credentials():
    if "gcp_service_account" in st.secrets:
        return service_account.Credentials.from_service_account_info(st.secrets["gcp_service_account"])
    return None

def init_vertex_ai():
    """Vertex AIの初期化"""
    creds = get_gcp_credentials()
    if creds:
        try:
            project_id = st.secrets["gcp_service_account"]["project_id"]
            # locationは us-central1 が最もモデル対応が早いです
            vertexai.init(project=project_id, location="us-central1", credentials=creds)
            return True
        except Exception as e:
            st.error(f"Vertex AI 初期化エラー: {e}")
            return False
    return False

# --- 教科書読み込み ---
@st.cache_resource
def upload_textbook_to_gemini():
    return []

# --- AI生成関数 (Vertex AI Gemini) ---
def safe_generate_content(content_text):
    if not init_vertex_ai():
        return "システムエラー: Vertex AI APIが無効か、認証に失敗しました。"

    # モデル名のリスト（エイリアスを使用）
    candidate_models = [
        "gemini-1.5-flash", # 最新のFlash
        "gemini-1.5-pro",   # 最新のPro
        "gemini-1.0-pro"    # 旧安定版
    ]
    
    last_error = ""
    for model_name in candidate_models:
        try:
            model = GenerativeModel(model_name)
            response = model.generate_content(
                content_text,
                generation_config={"temperature": 0.7, "max_output_tokens": 2048}
            )
            return response.text 
        except Exception as e:
            last_error = str(e)
            continue
            
    # 全モデル失敗時のエラー詳細
    return f"生成エラー: Vertex AIへの接続に失敗しました。\nヒント: Google Cloud Consoleで 'Vertex AI API' を有効にしてください。\n詳細: {last_error}"

# --- 音声合成 (Vertex AI / Cloud TTS) ---
def text_to_speech(text, speed=1.0, pitch=0.0):
    creds = get_gcp_credentials()
    if not creds: return None
    
    client = texttospeech.TextToSpeechClient(credentials=creds)
    synthesis_input = texttospeech.SynthesisInput(text=text)
    
    voice = texttospeech.VoiceSelectionParams(
        language_code="ja-JP",
        name="ja-JP-Neural2-B" 
    )
    
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speed,
        pitch=pitch
    )
    
    try:
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        return response.audio_content
    except Exception as e:
        st.error(f"音声合成エラー: {e}")
        return None

# --- Gemini 質問生成 ---
def get_opi_question(cefr, phase, history, info, textbook_files, exam_context):
    history_text = "\n".join([f"{h['role']}: {h['text']}" for h in history if h['role'] in ['examiner', 'student']])
    
    mode_instruction = ""
    if exam_context["is_exam"]:
        mode_instruction = f"これは試験です。対象: {exam_context['class']}。厳格に。"
    else:
        mode_instruction = "これは練習モードです。優しく会話をリードしてください。"

    prompt = f"""
    あなたは日本語会話の先生です。
    {mode_instruction}
    相手: {info['name']} (目標: {cefr})
    フェーズ: {OPI_PHASES[phase]}
    
    【履歴】
    {history_text}

    【指示】
    短く自然な日本語で質問してください（50文字以内推奨）。
    質問のみを出力してください。
    """
    
    return safe_generate_content(prompt)

# --- 評価生成 ---
def evaluate_response(question, answer, cefr, phase):
    prompt = f"""
    評価者として分析。
    目標:{cefr}, 質問:{question}, 回答:{answer}
    出力: Markdown箇条書きで 1.レベル判定 2.正確さ 3.助言
    """
    return safe_generate_content(prompt)

# --- 音声認識 (Vertex AI / Cloud Speech) ---
def speech_to_text(audio_bytes):
    creds = get_gcp_credentials()
    if not creds: return None, "認証エラー"
    client = speech.SpeechClient(credentials=creds)
    
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED,
        sample_rate_hertz=16000,
        language_code="ja-JP",
        enable_automatic_punctuation=True,
        model="latest_long"
    )
    try:
        audio = speech.RecognitionAudio(content=audio_bytes)
        res = client.recognize(config=config, audio=audio)
        if not res.results: return None, "聞き取れませんでした"
        return res.results[0].alternatives[0].transcript, None
    except Exception as e: return None, str(e)

# --- 保存処理 ---
def save_result(student_info, level, exam_context, history):
    creds = get_gcp_credentials()
    if not creds: return False, "認証エラー"
    sheet_url = exam_context.get("sheet_url")
    if not sheet_url: return False, "URL未設定"

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        client = gspread.authorize(creds.with_scopes(scope))
        sheet = client.open_by_url(sheet_url).sheet1
        
        exam_name = f"{exam_context['year']} {exam_context['type']}" if exam_context['is_exam'] else "練習"
        summary = safe_generate_content(f"会話ログから総評を100文字で:\n{str(history)}")
        
        row = [
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), 
            exam_name, exam_context.get('class', '-'), student_info['class'],
            student_info['id'], student_info['name'], level, summary
        ]
        sheet.append_row(row)
        return True, summary
    except Exception as e: return False, str(e)


# ==========================================
# UI & ロジック
# ==========================================

if "history" not in st.session_state: st.session_state.history = []
if "exam_state" not in st.session_state: st.session_state.exam_state = "setting"
if "phase_index" not in st.session_state: st.session_state.phase_index = 0
if "exam_config" not in st.session_state: st.session_state.exam_config = {"is_exam": False}
if "latest_audio" not in st.session_state: st.session_state.latest_audio = None
if "current_transcript" not in st.session_state: st.session_state.current_transcript = ""

# --- サイドバー ---
with st.sidebar:
    st.header("⚙️ システム設定 (Vertex AI)")
    mode = st.radio("モード", ["🐣 練習モード", "📝 試験モード"], index=0 if not st.session_state.exam_config["is_exam"] else 1)
    
    st.divider()
    st.subheader("🔊 音声設定")
    tts_speed = st.slider("話す速さ", 0.5, 2.0, 1.0, 0.1)
    tts_pitch = st.slider("声の高さ", -5.0, 5.0, 0.0, 1.0)

    if mode == "🐣 練習モード":
        st.session_state.exam_config = {"is_exam": False}
        st.info("Vertex AIモードで稼働中")
        
    elif mode == "📝 試験モード":
        st.divider()
        pwd = st.text_input("管理者パスワード", type="password")
        if pwd == ADMIN_PASSWORD:
            with st.form("exam_settings"):
                ex_year = st.number_input("年度", value=2025)
                ex_type = st.selectbox("種別", ["中間", "期末"])
                ex_class = st.text_input("クラス")
                ex_cefr = st.selectbox("レベル", ["A1", "A2", "B1", "B2"])
                ex_url = st.text_input("シートURL")
                
                if st.form_submit_button("設定"):
                    st.session_state.exam_config = {
                        "is_exam": True, "year": ex_year, "type": ex_type,
                        "class": ex_class, "level": ex_cefr, "sheet_url": ex_url
                    }
                    st.session_state.exam_state = "setting"
                    st.session_state.history = []
                    st.rerun()

    st.divider()
    if st.button("リセット"):
        st.session_state.clear()
        st.rerun()

# --- メインエリア ---
if st.session_state.exam_config["is_exam"]:
    conf = st.session_state.exam_config
    st.title(f"📝 {conf['year']} {conf['type']}")
else:
    st.title("🗣️ 日本語会話 (Vertex AI Mode)")

# 1. 設定画面
if st.session_state.exam_state == "setting":
    st.markdown("### 受験者情報を入力してください")
    c1, c2, c3 = st.columns(3)
    with c1: s_class = st.text_input("クラス")
    with c2: s_id = st.text_input("番号")
    with c3: s_name = st.text_input("氏名")
    
    if s_name:
        if st.button("確認して次へ", type="primary"):
            st.session_state.student_info = {"name": s_name, "class": s_class, "id": s_id}
            st.session_state.cefr_level = st.session_state.exam_config.get("level", "A2")
            st.session_state.phase_index = 0
            st.session_state.exam_state = "ready"
            st.rerun()

# 2. 開始待機画面
elif st.session_state.exam_state == "ready":
    st.markdown(f"## こんにちは、{st.session_state.student_info['name']} さん。")
    st.divider()
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("🔴 試験を開始する", type="primary", use_container_width=True):
            st.session_state.exam_state = "interview"
            current = PHASE_ORDER[0]
            with st.spinner("AIが質問を生成しています..."):
                q = get_opi_question(st.session_state.cefr_level, current, [], st.session_state.student_info, [], st.session_state.exam_config)
                st.session_state.history.append({"role": "examiner", "text": q, "phase": current})
                audio_data = text_to_speech(q, tts_speed, tts_pitch)
                st.session_state.latest_audio = audio_data
                st.rerun()

# 3. 会話画面
elif st.session_state.exam_state == "interview":
    prog = (st.session_state.phase_index + 1) / len(PHASE_ORDER)
    st.progress(prog)
    
    last_q = st.session_state.history[-1]["text"]
    
    st.markdown(f"""
    <div style="background-color:#e8f0fe;padding:20px;border-radius:10px;margin-bottom:20px;">
        <h3 style="margin:0;">👮 先生: {last_q}</h3>
    </div>
    """, unsafe_allow_html=True)

    if st.session_state.latest_audio:
        st.audio(st.session_state.latest_audio, format="audio/mp3", autoplay=True)
    
    with st.expander("これまでの会話履歴"):
        for chat in st.session_state.history[:-1]:
            role = "👮" if chat["role"]=="examiner" else "🧑‍🎓"
            st.write(f"{role}: {chat['text']}")

    st.markdown("---")
    
    current_key = f"audio_recorder_{st.session_state.phase_index}"
    audio_val = st.audio_input("録音ボタンを押して話し、停止ボタンを押してください（自動送信）", key=current_key)
    
    if audio_val:
        with st.status("🔄 音声を解析して、AIに送信しています...", expanded=True) as status:
            
            st.write("📂 音声データを変換中...")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
                tmp.write(audio_val.getvalue())
                webm_path = tmp.name
            mp3_path = webm_path + ".mp3"
            os.system(f'ffmpeg -y -i "{webm_path}" -ac 1 -ar 16000 -ab 32k "{mp3_path}" -loglevel quiet')
            
            st.write("🎧 音声を文字に起こしています (Vertex AI)...")
            with open(mp3_path, "rb") as f: content = f.read()
            text, err = speech_to_text(content)
            try: os.remove(webm_path); os.remove(mp3_path)
            except: pass
            
            if text:
                st.write(f"📝 聞き取り完了: 「{text}」")
                st.write("🤖 Vertex AIが回答と評価を生成中...")
                
                st.session_state.history.append({"role": "student", "text": text})
                
                current_phase_key = PHASE_ORDER[st.session_state.phase_index]
                eval_text = evaluate_response(last_q, text, st.session_state.cefr_level, current_phase_key)
                st.session_state.history.append({"role": "grade", "text": eval_text})
                
                st.session_state.phase_index += 1
                if st.session_state.phase_index < len(PHASE_ORDER):
                    next_p = PHASE_ORDER[st.session_state.phase_index]
                    next_q = get_opi_question(st.session_state.cefr_level, next_p, st.session_state.history, st.session_state.student_info, [], st.session_state.exam_config)
                    st.session_state.history.append({"role": "examiner", "text": next_q, "phase": next_p})
                    
                    st.write("🗣️ 次の音声を生成中...")
                    next_audio = text_to_speech(next_q, tts_speed, tts_pitch)
                    st.session_state.latest_audio = next_audio
                    
                    status.update(label="完了！次の質問へ進みます", state="complete", expanded=False)
                    time.sleep(1)
                    st.rerun()
                else:
                    st.session_state.exam_state = "finished"
                    st.rerun()
            else:
                status.update(label="聞き取れませんでした", state="error")
                st.error("音声が聞き取れませんでした。もう一度録音してください。")

# 4. 終了
elif st.session_state.exam_state == "finished":
    st.balloons()
    st.success("試験終了！")
    if "saved" not in st.session_state:
        ok, msg = save_result(st.session_state.student_info, st.session_state.cefr_level, st.session_state.exam_config, st.session_state.history)
        st.session_state.saved = True
        if ok: st.info(f"保存完了: {msg}")
    
    if st.button("トップへ戻る"):
        st.session_state.clear()
        st.rerun()
