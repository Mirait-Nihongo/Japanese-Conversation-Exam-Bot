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
st.set_page_config(page_title="日本語発音 指導補助ツール v2", page_icon="👨‍🏫", layout="centered")
st.title("👨‍🏫 日本語発音 指導補助ツール v2") # ★タイトルを変えました（更新確認用）
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
        available_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        if not available_models:
            return "❌ エラー: 利用可能なGeminiモデルが見つかりません。"

        target_model = available_models[0]
        for m in available_models:
            if "gemini-1.5-flash" in m:
                target_model = m
                break
            elif "gemini-pro" in m:
                target_model = m
        
        model = genai.GenerativeModel(target_model)
        
        name_part = f"学習者名は「{student_name}」です。" if student_name else "学習者名は不明です。"
        
        if nationality:
            nat_instruction = f"学習者の母語・国籍は「{nationality}」です。この言語と日本語の対照言語学的視点から分析してください。"
        else:
            nat_instruction = "母語情報は不明です。一般的な誤用分析を行ってください。"

        prompt = f"""
        あなたは日本語音声学・対照言語学・日本語教育の高度な専門家です。
        以下の音声認識データに基づき、教師が指導に活用するための詳細な「発音診断カルテ」を作成してください。

        【基本情報】
        {name_part}
        {nat_instruction}
        
        【分析対象データ】
        ※データ内の「⚠️」は、機械判定の信頼度が低い（不明瞭または誤音の可能性が高い）箇所です。
        1. 認識結果: {text}
        2. 揺れ(別候補): {alts}
        3. 詳細スコア: {details}

        【必須分析項目】
        以下の5つの観点を必ず含めてレポートを作成してください。

        1. **音韻体系の対照分析**
           - {nationality if nationality else "学習者の母語"}の音韻体系と日本語の相違点に基づく全体的傾向
        
        2. **母語にない・区別されない日本語音**
           - 母語に存在しないため代用されている音、統合されてしまっている音の指摘
           - (例: 清濁、有気・無気、特定の母音など)

        3. **調音位置・調音方法のずれ**
           - 具体的な調音点（舌の位置、唇の形）や調音方法（閉鎖、摩擦の強さ）の誤り
           - ⚠️が付いている箇所を中心に、どのような物理的ズレが起きているか推測してください

        4. **知覚上の誤認（聞き分けの問題）**
           - 発音の誤りが「音を聞き分けられていない」ことに起因する可能性の分析
           - 「揺れ（別候補）」データから、学習者がどの音と混同しているか分析

        5. **日本語特有のプロソディ**
           - 拍（モーラ）感覚、長音、促音（っ）、撥音（ん）のリズム
           - ピッチアクセントとイントネーションの自然さ

        【出力形式】
        見出しを付けて構造化し、専門用語には教師向けの簡単な補足を加えてください。
        最後に「最優先指導計画」を提案してください。
        """
        response = model.generate_content(prompt)
        return response.text

    except Exception as e:
        return f"❌ 予期せぬエラー: {e}"

# --- メイン画面 ---
st.info("👇 学習者の情報を入力してください")

# ★★★ ここが入力欄を2つにするコードです ★★★
col1, col2 = st.columns(2)

with col1:
    student_name = st.text_input("学習者氏名", placeholder="例: ジョン・スミス")

with col2:
    nationality = st.text_input("母語・国籍 (分析に必須)", placeholder="例: ベトナム語、中国語、英語")
# ★★★★★★★★★★★★★★★★★★★★★★★

tab1, tab2 = st.tabs(["📁 ファイルをアップロード", "🎙️ その場で録音する"])

target_audio = None 

with tab1:
    uploaded_file = st.file_uploader("音声ファイルを選択 (mp3, wav, m4a)", type=["mp3", "wav", "m4a"])
    if uploaded_file:
        st.audio(uploaded_file)
        target_audio = uploaded_file

with tab2:
    st.write("ボタンを押して話し、終わったら停止ボタンを押してください。")
    recorded_audio = st.audio_input("録音開始")
    if recorded_audio:
        target_audio = recorded_audio

# --- 分析ボタン ---
if st.button("🚀 専門分析を開始する", type="primary"):
    if target_audio:
        with st.spinner('🎧 対照言語学的分析を実行中...'):
            audio_bytes = target_audio.getvalue()

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_audio:
                tmp_audio.write(audio_bytes)
                tmp_audio_path = tmp_audio.name
            
            res = analyze_audio(tmp_audio_path)
            
            if "error" in res:
                st.error(res["error"])
            else:
                st.success("解析完了")

                # 固定プレーヤー
                player_html = get_sticky_audio_player(audio_bytes)
                st.markdown(player_html, unsafe_allow_html=True)

                st.subheader("🗣️ 音声認識データ")
                st.code(res["main_text"], language=None)
                
                with st.expander("🔍 分析用生データ (教師用)", expanded=True):
                    st.write("※スコアが80未満の箇所には ⚠️ が付いています")
                    st.write(f"信頼度詳細: {res['details']}")
                    st.write(f"別候補: {res['alts']}")

                st.markdown("---")
                
                title_suffix = f" ({nationality})" if nationality else ""
                name_display = student_name if student_name else "学習者"
                st.subheader(f"📝 {name_display}さんの発音診断カルテ{title_suffix}")
                
                report_content = ask_gemini(student_name, nationality, res["main_text"], res["alts"], res["details"])
                st.markdown(report_content)
                
                today_str = datetime.datetime.now().strftime('%Y-%m-%d')
                safe_name = student_name if student_name else "student"
                safe_nat = nationality if nationality else "unknown"
                
                download_text = f"""================================
日本語発音診断レポート
================================
■ 実施日: {today_str}
■ 学習者: {safe_name}
■ 母語・国籍: {safe_nat}

【音声認識結果】
{res['main_text']}

【詳細スコア (信頼度)】
※80点未満は ⚠️ マーク付き
{res['details']}

【認識候補の揺れ】
{res['alts']}

--------------------------------
【AI講師による詳細診断（5つの観点）】
--------------------------------
{report_content}
"""
                file_name = f"{safe_name}_{today_str}_report.txt"

                st.download_button(
                    label="📥 診断結果をテキストで保存",
                    data=download_text,
                    file_name=file_name,
                    mime="text/plain"
                )

            if os.path.exists(tmp_audio_path): os.remove(tmp_audio_path)
    else:
        st.warning("音声ファイルを選択するか、録音してください。")
