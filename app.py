import streamlit as st
import os
import io
import tempfile
import datetime
import base64
import google.generativeai as genai
from google.cloud import speech
from google.oauth2 import service_account

# --- 設定 ---
st.set_page_config(page_title="日本語発音 指導補助ツール", page_icon="👨‍🏫", layout="centered")
st.title("👨‍🏫 日本語発音 指導補助ツール")
st.markdown("教師向け：対照言語学に基づく発音診断・誤用分析")

# --- 認証情報の読み込み ---
try:
    gemini_api_key = st.secrets["GEMINI_API_KEY"]
    google_json_str = st.secrets["GOOGLE_JSON"]
    
    genai.configure(api_key=gemini_api_key)
    
    with open("google_key.json", "w") as f:
        f.write(google_json_str)
    json_path = "google_key.json"
except Exception as e:
    st.error("⚠️ 設定エラー: Secretsの設定を確認してください。")
    st.stop()

# --- 関数群 ---

def get_sticky_audio_player(audio_bytes):
    """音声データをBase64に変換して、画面下に固定されるHTMLプレーヤーを作る"""
    b64 = base64.b64encode(audio_bytes).decode()
    md = f"""
        <style>
            .sticky-audio {{
                position: fixed;
                bottom: 0;
                left: 0;
                width: 100%;
                background-color: #f0f2f6;
                padding: 10px 20px;
                z-index: 99999;
                border-top: 1px solid #ccc;
                text-align: center;
                box-shadow: 0px -2px 10px rgba(0,0,0,0.1);
            }}
            .main .block-container {{
                padding-bottom: 100px;
            }}
        </style>
        <div class="sticky-audio">
            <div style="margin-bottom:5px; font-weight:bold; font-size:0.9em; color:#333;">
                🔊 録音データ再生（診断カルテを見ながら聞いてください）
            </div>
            <audio controls src="data:audio/mp3;base64,{b64}" style="width: 100%; max-width: 600px;"></audio>
        </div>
    """
    return md

def analyze_audio(audio_path):
    try:
        credentials = service_account.Credentials.from_service_account_file(json_path)
        client = speech.SpeechClient(credentials=credentials)
    except Exception as e:
        return {"error": f"認証エラー: {e}"}

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_converted:
        converted_path = tmp_converted.name
    
    cmd = f'ffmpeg -y -i "{audio_path}" -ac 1 -ar 16000 -ab 32k "{converted_path}" -loglevel panic'
    exit_code = os.system(cmd)
    
    if exit_code != 0:
        return {"error": "音声変換エラー"}

    with io.open(converted_path, "rb") as f:
        content = f.read()
    
    try:
        audio = speech.RecognitionAudio(content=content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED,
            sample_rate_hertz=16000,
            language_code="ja-JP",
            enable_automatic_punctuation=False,
            max_alternatives=5, 
            enable_word_confidence=True
        )
        operation = client.long_running_recognize(config=config, audio=audio)
        response = operation.result(timeout=600)
    except Exception as e:
        return {"error": f"認識エラー: {e}"}
    finally:
        if os.path.exists(converted_path): os.remove(converted_path)

    if not response.results:
        return {"error": "音声認識不可(無音/ノイズ)"}

    result = response.results[0]
    alt = result.alternatives[0]
    all_candidates = [a.transcript for a in result.alternatives]
    
    details_list = []
    for w in alt.words:
        score = int(w.confidence * 100)
        marker = " ⚠️" if w.confidence < 0.8 else ""
        details_list.append(f"{w.word}({score}){marker}")
    
    formatted_details = ", ".join(details_list)

    return {
        "main_text": alt.transcript,
        "alts": ", ".join(all_candidates),
        "details": formatted_details
    }

def ask_gemini(student_name, nationality, text, alts, details):
    try:
