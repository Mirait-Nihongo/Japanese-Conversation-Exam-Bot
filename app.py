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
    
    voice = texttospeech.VoiceSelectionParams(
        language_code="ja-JP",
        name="ja-JP-Neural2-B" 
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
    if not sheet_url: return False, "URL未設定
