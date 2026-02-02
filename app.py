import streamlit as st
import os
import time
import tempfile
import datetime
import google.generativeai as genai
from google.cloud import speech
from google.oauth2 import service_account
import gspread
import importlib.metadata # バージョン確認用

# --- ページ設定 ---
st.set_page_config(page_title="日本語会話試験システム", page_icon="🏫", layout="wide")

# --- 定数・初期設定 ---
MATERIALS_DIR = "materials"
OPI_PHASES = {
    "warmup": "導入 (Warm-up)",
    "level_check": "レベルチェック",
    "probe": "突き上げ (Probe)",
    "wind_down": "終結 (Wind-down)"
}
PHASE_ORDER = ["warmup", "level_check", "level_check", "probe", "wind_down"]

# 管理者パスワード (Secretsになければデフォルト 'admin')
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
    if not os.path.exists(MATERIALS_DIR): os.makedirs(MATERIALS_DIR)
    uploaded_files = []
    for file in os.listdir(MATERIALS_DIR):
        if file.lower().endswith(".pdf"):
            try:
                g_file = genai.upload_file(os.path.join(MATERIALS_DIR, file))
                while g_file.state.name == "PROCESSING": time.sleep(1); g_file = genai.get_file(g_file.name)
                if g_file.state.name == "ACTIVE": uploaded_files.append(g_file)
            except: pass
    return uploaded_files

# --- 安全な生成関数 (診断済みモデルを使用) ---
def safe_generate_content(prompt_content):
    # 診断画面で存在が確認された最も標準的なモデル名を使用
    target_model = "gemini-1.5-flash" 
    
    try:
        model = genai.GenerativeModel(target_model)
        return model.generate_content(prompt_content).text
    except Exception as e:
        # 万が一のエラー時はProモデルにフォールバック
        try:
            model = genai.GenerativeModel("gemini-1.5-pro")
            return model.generate_content(prompt_content).text
        except:
            return f"生成エラー: {e}"

# --- Gemini 質問生成 ---
def get_opi_question(cefr, phase, history, info, textbook_files, exam_context):
    history_text = "\n".join([f"{h['role']}: {h['text']}" for h in history if h['role'] in ['examiner', 'student']])
    
    mode_instruction = ""
    if exam_context["is_exam"]:
        mode_instruction = f"""
        これは「{exam_context['year']}年度 {exam_context['type']}」の試験です。
        対象クラス: {exam_context['class']}
        厳格な試験官として振る舞ってください。
        """
    else:
        mode_instruction = "これは練習モードです。優しく指導してください。"

    prompt = f"""
    あなたはOPI準拠の日本語会話テスターです。
    {mode_instruction}
    
    学習者: {info['name']} (目標: {cefr})
    現在のフェーズ: {OPI_PHASES[phase]}
    
    【これまでの会話】
    {history_text}

    【指示】
    1. 提供された教科書資料の内容（語彙・文型）を活用して質問を作成してください。
    2. フェーズ進行({OPI_PHASES[phase]})を厳守してください。
    3. 質問文のみを出力してください。
    """
    
    content = [prompt]
    if textbook_files: content.extend(textbook_files)
    
    return safe_generate_content(content)

# --- 評価生成 ---
def evaluate_response(question, answer, cefr, phase):
    prompt = f"""
    評価者として分析してください。
    目標: {cefr}, フェーズ: {phase}
    質問: {question}
    回答: {answer}
    出力: Markdown箇条書きで 1.レベル判定(達成/未達) 2.文法・語彙の正確さ 3.アドバイス
    """
    return safe_generate_content(prompt)

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

# --- 保存処理 (URL対応版) ---
def save_result(student_info, level, exam_context, history):
    creds = get_gcp_credentials()
    if not creds: return False, "認証エラー"
    
    sheet_url = exam_context.get("sheet_url")
    if not sheet_url:
        return False, "スプレッドシートのURLが設定されていません。管理者に連絡してください。"

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        client = gspread.authorize(creds.with_scopes(scope))
        
        sheet = client.open_by_url(sheet_url).sheet1
        
        exam_name = f"{exam_context['year']} {exam_context['type']}" if exam_context['is_exam'] else "練習モード"
        
        summary = safe_generate_content(f"以下の会話ログから総評を100文字で:\n{str(history)}")
        
        row = [
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), 
            exam_name,
            exam_context.get('class', '-'),
            student_info['class'],
            student_info['id'], 
            student_info['name'], 
            level, 
            summary
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

# --- サイドバー ---
with st.sidebar:
    st.header("⚙️ システム設定")
    # バージョン表示（念のため残しておきます）
    try:
        ver = importlib.metadata.version("google-generativeai")
        st.caption(f"Ver: {ver}")
    except: pass

    mode = st.radio("モード選択", ["🐣 練習モード", "📝 試験モード"], index=0 if not st.session_state.exam_config["is_exam"] else 1)
    
    if mode == "🐣 練習モード":
        st.session_state.exam_config = {"is_exam": False}
        st.info("自習用のモードです。")
        
    elif mode == "📝 試験モード":
        st.divider()
        st.subheader("🔒 試験設定 (先生用)")
        pwd = st.text_input("管理者パスワード", type="password")
        
        if pwd == ADMIN_PASSWORD:
            st.success("設定可能")
            with st.form("exam_settings"):
                st.markdown("#### 1. 試験情報の入力")
                this_year = datetime.date.today().year
                ex_year = st.number_input("年度", min_value=2024, max_value=2030, value=this_year)
                ex_type = st.selectbox("試験種別", ["1学期中間試験", "1学期期末試験", "2学期中間試験", "2学期期末試験", "学年末試験", "卒業試験"])
                ex_class = st.text_input("対象クラス", placeholder="例: 2年A組")
                ex_cefr = st.selectbox("試験レベル (CEFR)", ["A1", "A2", "B1", "B2", "C1", "C2"])
                
                st.markdown("#### 2. 結果保存先")
                ex_sheet_url = st.text_input(
                    "スプレッドシートのURL", 
                    placeholder="https://docs.google.com/spreadsheets/...",
                    help="作成したスプレッドシートのURLを貼り付けてください"
                )
                
                if st.form_submit_button("試験設定を適用・ロックする"):
                    if not ex_sheet_url:
                        st.error("URLを入力してください")
                    else:
                        st.session_state.exam_config = {
                            "is_exam": True,
                            "year": ex_year,
                            "type": ex_type,
                            "class": ex_class,
                            "level": ex_cefr,
                            "sheet_url": ex_sheet_url
                        }
                        st.toast("試験モードを開始しました！")
                        st.session_state.exam_state = "setting"
                        st.session_state.history = []
                        st.rerun()
        else:
            if st.session_state.exam_config.get("is_exam"):
                st.info("試験モードで稼働中")
            else:
                st.warning("設定にはパスワードが必要です")

    st.divider()
    if configure_gemini():
        with st.spinner("資料読込中..."):
            textbooks = upload_textbook_to_gemini()
    
    if st.button("リセット"):
        st.session_state.clear()
        st.rerun()

# --- メインエリア ---
if st.session_state.exam_config["is_exam"]:
    conf = st.session_state.exam_config
    st.title(f"📝 {conf['year']}年度 {conf['type']}")
    st.markdown(f"**対象クラス:** {conf['class']}　|　**試験レベル:** {conf['level']}")
    st.divider()
else:
    st.title("🗣️ 日本語会話練習 (Practice)")

# 1. 開始前画面
if st.session_state.exam_state == "setting":
    if not st.session_state.exam_config["is_exam"]:
        col1, col2 = st.columns(2)
        with col1:
            s_class = st.text_input("クラス", placeholder="例: 2年A組")
            s_id = st.text_input("学籍番号", placeholder="例: L2025-001")
        with col2:
            s_name = st.text_input("氏名", placeholder="例: 山田 花子")
            selected_cefr = st.selectbox("練習レベル", ["A1", "A2", "B1", "B2", "C1", "C2"])
            
        if st.button("練習を開始する", type="primary"):
            if not s_name: st.error("名前を入力してください")
            else:
                st.session_state.student_info = {"name": s_name, "class": s_class, "id": s_id}
                st.session_state.cefr_level = selected_cefr
                st.session_state.phase_index = 0
                st.session_state.exam_state = "interview"
                current = PHASE_ORDER[0]
                q = get_opi_question(selected_cefr, current, [], st.session_state.student_info, textbooks, st.session_state.exam_config)
                st.session_state.history.append({"role": "examiner", "text": q, "phase": current})
                st.rerun()
    else:
        st.markdown("以下の情報を入力してください。")
        with st.container(border=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                default_cls = st.session_state.exam_config.get("class", "")
                s_class = st.text_input("クラス", value=default_cls)
            with c2: s_id = st.text_input("学籍番号", placeholder="例: 15")
            with c3: s_name = st.text_input("氏名", placeholder="例: 山田 太郎")
        
        if not s_name or not s_id:
            st.warning("⚠️ 全ての項目を入力すると、開始ボタンが表示されます。")
        else:
            if st.button("🚀 試験を開始する", type="primary", use_container_width=True):
                st.session_state.student_info = {"name": s_name, "class": s_class, "id": s_id}
                st.session_state.cefr_level = st.session_state.exam_config["level"]
                st.session_state.phase_index = 0
                st.session_state.exam_state = "interview"
                current = PHASE_ORDER[0]
                with st.spinner("試験問題を生成中..."):
                    q = get_opi_question(st.session_state.cefr_level, current, [], st.session_state.student_info, textbooks, st.session_state.exam_config)
                    st.session_state.history.append({"role": "examiner", "text": q, "phase": current})
                    st.rerun()

# 2. 面接画面
elif st.session_state.exam_state == "interview":
    st.caption(f"受験者: {st.session_state.student_info['class']} {st.session_state.student_info['name']}")
    prog = (st.session_state.phase_index + 1) / len(PHASE_ORDER)
    st.progress(prog)
    st.caption(f"フェーズ: {OPI_PHASES[PHASE_ORDER[st.session_state.phase_index]]}")

    for chat in st.session_state.history:
        role = chat["role"]
        if role == "examiner": st.info(f"👮: {chat['text']}")
        elif role == "student": st.success(f"🧑‍🎓: {chat['text']}")
        elif role == "grade": 
            with st.expander("📝 評価"): st.markdown(chat['text'])

    if st.session_state.history[-1]["role"] == "examiner":
        audio_val = st.audio_input("録音ボタンを押して回答してください")
        if audio_val:
            with st.spinner("送信中..."):
                # 一時ファイル作成
                with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
                    tmp.write(audio_val.getvalue())
                    webm_path = tmp.name
                
                mp3_path = webm_path + ".mp3"
                
                # FFmpegで変換
                # -y: 上書き許可, -loglevel error: エラーのみ表示
                cmd_res = os.system(f'ffmpeg -y -i "{webm_path}" -ac 1 -ar 16000 -ab 32k "{mp3_path}" -loglevel error')
                
                if cmd_res != 0 or not os.path.exists(mp3_path):
                    st.error("音声変換に失敗しました。FFmpegがインストールされているか確認してください。")
                else:
                    with open(mp3_path, "rb") as f:
                        content = f.read()
                    
                    text, err = speech_to_text(content)
                    
                    # 後始末
                    try:
                        os.remove(webm_path)
                        os.remove(mp3_path)
                    except:
                        pass

                    if text:
                        st.session_state.history.append({"role": "student", "text": text})
                        last_q = st.session_state.history[-2]["text"]
                        phase = st.session_state.history[-2]["phase"]
                        eval_text = evaluate_response(last_q, text, st.session_state.cefr_level, phase)
                        st.session_state.history.append({"role": "grade", "text": eval_text})
                        st.session_state.phase_index += 1
                        
                        if st.session_state.phase_index < len(PHASE_ORDER):
                            next_p = PHASE_ORDER[st.session_state.phase_index]
                            next_q = get_opi_question(st.session_state.cefr_level, next_p, st.session_state.history, st.session_state.student_info, textbooks, st.session_state.exam_config)
                            st.session_state.history.append({"role": "examiner", "text": next_q, "phase": next_p})
                            st.rerun()
                        else:
                            st.session_state.exam_state = "finished"
                            st.rerun()
                    else:
                        st.error(f"音声認識エラー: {err}")

# 3. 終了画面
elif st.session_state.exam_state == "finished":
    st.balloons()
    st.success("試験終了です。お疲れ様でした。")
    if "saved" not in st.session_state:
        with st.spinner("結果を保存中..."):
            ok, msg = save_result(st.session_state.student_info, st.session_state.cefr_level, st.session_state.exam_config, st.session_state.history)
            if ok: st.success("✅ データが送信されました"); st.session_state.saved = True
            else: st.error(f"保存エラー: {msg}")
    if st.button("終了（トップ画面へ）"):
        st.session_state.clear()
        st.rerun()
