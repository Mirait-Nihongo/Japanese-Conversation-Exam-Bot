import streamlit as st
import os
import time
import tempfile
import datetime
import google.generativeai as genai
from google.cloud import speech
from google.cloud import texttospeech
from google.oauth2 import service_account
import gspread
import importlib.metadata

# --- ページ設定 ---
st.set_page_config(page_title="日本語会話試験システム", page_icon="🎙️", layout="wide")

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

# --- 認証関係 ---
def get_gcp_credentials():
    if "gcp_service_account" in st.secrets:
        return service_account.Credentials.from_service_account_info(st.secrets["gcp_service_account"])
    return None

def configure_gemini():
    if "GEMINI_API_KEY" in st.secrets:
        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
        return True
    return False

# --- 教科書読み込み ---
@st.cache_resource
def upload_textbook_to_gemini():
    if not configure_gemini(): return [] 
    if not os.path.exists(MATERIALS_DIR): os.makedirs(MATERIALS_DIR)
    uploaded_files = []
    for file in os.listdir(MATERIALS_DIR):
        if file.lower().endswith(".pdf"):
            try:
                g_file = genai.upload_file(os.path.join(MATERIALS_DIR, file))
                while g_file.state.name == "PROCESSING": 
                    time.sleep(1)
                    g_file = genai.get_file(g_file.name)
                if g_file.state.name == "ACTIVE": 
                    uploaded_files.append(g_file)
            except: pass
    return uploaded_files

# --- AI生成関数 (Gemini 2.0 Flash優先) ---
def safe_generate_content(content_data):
    configure_gemini()
    # あなたの環境で成功したモデル順
    candidate_models = [
        "models/gemini-2.0-flash",       
        "gemini-2.0-flash",              
        "models/gemini-1.5-flash",       
        "models/gemini-pro"
    ]
    last_error = ""
    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(content_data)
            return response.text 
        except Exception as e:
            last_error = str(e)
            continue
    return f"生成エラー: 接続失敗。詳細: {last_error}"

# --- 音声合成 (Text-to-Speech) ---
def text_to_speech(text):
    creds = get_gcp_credentials()
    if not creds: return None
    
    client = texttospeech.TextToSpeechClient(credentials=creds)
    synthesis_input = texttospeech.SynthesisInput(text=text)
    
    # Gemini Live風の高品質な声 (Neural2)
    voice = texttospeech.VoiceSelectionParams(
        language_code="ja-JP",
        name="ja-JP-Neural2-B" # B:女性, C:男性, D:男性(低音)
    )
    
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
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
    
    content = [prompt]
    if textbook_files: content.extend(textbook_files)
    
    return safe_generate_content(content)

# --- 評価生成 ---
def evaluate_response(question, answer, cefr, phase):
    prompt = f"""
    評価者として分析。
    目標:{cefr}, 質問:{question}, 回答:{answer}
    出力: Markdown箇条書きで 1.レベル判定 2.正確さ 3.助言
    """
    return safe_generate_content([prompt])

# --- 音声認識 ---
def speech_to_text(audio_bytes):
    creds = get_gcp_credentials()
    if not creds: return None, "認証エラー"
    client = speech.SpeechClient(credentials=creds)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED,
        sample_rate_hertz=16000,
        language_code="ja-JP",
        enable_automatic_punctuation=True
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
        summary = safe_generate_content([f"会話ログから総評を100文字で:\n{str(history)}"])
        
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

# --- サイドバー ---
with st.sidebar:
    st.header("⚙️ システム設定")
    mode = st.radio("モード", ["🐣 練習モード", "📝 試験モード"], index=0 if not st.session_state.exam_config["is_exam"] else 1)
    
    if mode == "🐣 練習モード":
        st.session_state.exam_config = {"is_exam": False}
        st.info("AIが声で話しかけます。")
        
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
    if configure_gemini():
        upload_textbook_to_gemini()
    if st.button("リセット"):
        st.session_state.clear()
        st.rerun()

# --- メインエリア ---
if st.session_state.exam_config["is_exam"]:
    conf = st.session_state.exam_config
    st.title(f"📝 {conf['year']} {conf['type']}")
else:
    st.title("🗣️ 日本語会話 (Gemini Live Mode)")

# 1. 設定画面
if st.session_state.exam_state == "setting":
    c1, c2, c3 = st.columns(3)
    with c1: s_class = st.text_input("クラス")
    with c2: s_id = st.text_input("番号")
    with c3: s_name = st.text_input("氏名")
    
    if s_name:
        if st.button("会話スタート", type="primary"):
            st.session_state.student_info = {"name": s_name, "class": s_class, "id": s_id}
            st.session_state.cefr_level = st.session_state.exam_config.get("level", "A2")
            st.session_state.phase_index = 0
            st.session_state.exam_state = "interview"
            
            # 最初の質問生成
            current = PHASE_ORDER[0]
            with st.spinner("AIが準備中..."):
                q = get_opi_question(st.session_state.cefr_level, current, [], st.session_state.student_info, [], st.session_state.exam_config)
                st.session_state.history.append({"role": "examiner", "text": q, "phase": current})
                # 音声生成
                audio_data = text_to_speech(q)
                st.session_state.latest_audio = audio_data
                st.rerun()

# 2. 会話画面 (Gemini Live風)
elif st.session_state.exam_state == "interview":
    # 進捗バー
    prog = (st.session_state.phase_index + 1) / len(PHASE_ORDER)
    st.progress(prog)
    
    # 最新の質問を表示
    last_q = st.session_state.history[-1]["text"]
    
    # --- ここがGemini Live風のポイント ---
    # 先生の顔アイコンと質問
    st.markdown(f"""
    <div style="background-color:#e8f0fe;padding:20px;border-radius:10px;margin-bottom:20px;">
        <h3 style="margin:0;">👮 先生: {last_q}</h3>
    </div>
    """, unsafe_allow_html=True)

    # ★自動再生 (Autoplay)
    if st.session_state.latest_audio:
        st.audio(st.session_state.latest_audio, format="audio/mp3", autoplay=True)
        # 一度再生したらクリアしないとリロードで何度も喋ってしまうので注意が必要だが、
        # Streamlitの仕組み上、次の入力まで保持させる
    
    # 履歴表示（折りたたみ）
    with st.expander("これまでの会話履歴を見る"):
        for chat in st.session_state.history[:-1]:
            role = "👮" if chat["role"]=="examiner" else "🧑‍🎓"
            st.write(f"{role}: {chat['text']}")
            if chat["role"]=="grade": st.caption(f"📝 {chat['text']}")

    # 音声入力エリア
    audio_val = st.audio_input("マイクボタンを押して返事してください")
    
    if audio_val:
        with st.spinner("聞いています..."):
            # WebM -> MP3変換
            with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
                tmp.write(audio_val.getvalue())
                webm_path = tmp.name
            mp3_path = webm_path + ".mp3"
            os.system(f'ffmpeg -y -i "{webm_path}" -ac 1 -ar 16000 -ab 32k "{mp3_path}" -loglevel quiet')
            
            with open(mp3_path, "rb") as f: content = f.read()
            text, err = speech_to_text(content)
            try: os.remove(webm_path); os.remove(mp3_path)
            except: pass

            if text:
                # 生徒の回答を保存
                st.session_state.history.append({"role": "student", "text": text})
                
                # 評価と次の質問
                phase = st.session_state.history[-2]["phase"]
                eval_text = evaluate_response(last_q, text, st.session_state.cefr_level, phase)
                st.session_state.history.append({"role": "grade", "text": eval_text})
                
                st.session_state.phase_index += 1
                if st.session_state.phase_index < len(PHASE_ORDER):
                    next_p = PHASE_ORDER[st.session_state.phase_index]
                    next_q = get_opi_question(st.session_state.cefr_level, next_p, st.session_state.history, st.session_state.student_info, [], st.session_state.exam_config)
                    st.session_state.history.append({"role": "examiner", "text": next_q, "phase": next_p})
                    
                    # 次の音声を生成
                    next_audio = text_to_speech(next_q)
                    st.session_state.latest_audio = next_audio
                    st.rerun()
                else:
                    st.session_state.exam_state = "finished"
                    st.rerun()
            else:
                st.warning("音声が聞き取れませんでした。もう一度お願いします。")

# 3. 終了
elif st.session_state.exam_state == "finished":
    st.balloons()
    st.success("試験終了！")
    if "saved" not in st.session_state:
        ok, msg = save_result(st.session_state.student_info, st.session_state.cefr_level, st.session_state.exam_config, st.session_state.history)
        st.session_state.saved = True
        if ok: st.info(f"保存完了: {msg}")
    
    if st.button("最初に戻る"):
        st.session_state.clear()
        st.rerun()
