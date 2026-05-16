import os
import re
import uuid
import time
import json
import shutil
import subprocess
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)

from telegram.error import TimedOut, NetworkError

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

IDS_LIBERADOS = {
    8672397104,
    1130170420,
}

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

usuarios = {}


def autorizado(user_id):
    return user_id in IDS_LIBERADOS


def painel_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 Revisar / Limpar EPUB", callback_data="modo_revisar")],
        [InlineKeyboardButton("🤖 Revisar com Gemini", callback_data="modo_gemini")],
        [InlineKeyboardButton("🖼 Editar capa", callback_data="modo_capa")],
        [InlineKeyboardButton("🔄 Conversor Alma Scriptum", callback_data="modo_conversor")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ])


def painel_conversor():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 EPUB → PDF", callback_data="conv_epub_pdf")],
        [InlineKeyboardButton("📄 PDF → EPUB", callback_data="conv_pdf_epub")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="voltar")],
    ])


def barra_progresso(porcentagem):
    cheios = porcentagem // 10
    vazios = 10 - cheios
    return "🟩" * cheios + "⬜" * vazios


async def atualizar_carregamento(mensagem, titulo, porcentagem, status):
    try:
        await mensagem.edit_text(
            f"{titulo}\n\n"
            f"📊 Progresso: {porcentagem}%\n"
            f"{barra_progresso(porcentagem)}\n\n"
            f"{status}"
        )
    except Exception:
        pass


def limpar_nome(nome):
    nome = Path(nome).stem
    nome = nome.replace("_", " ").replace("-", " ")
    nome = re.sub(r"\s*\([^)]*\)", " ", nome)

    sujeiras = [
        r"oceanofpdf\.com", r"oceanofpdf", r"ocean of pdf", r"oceanpdf",
        r"z-library\.sk", r"z-library", r"zlib", r"z-lib",
        r"1lib\.sk", r"1lib", r"library",
        r"traduzido", r"ptbr", r"pt-br", r"\[pt-br\]",
        r"alma scriptum", r"studio",
    ]

    for s in sujeiras:
        nome = re.sub(s, " ", nome, flags=re.I)

    nome = re.sub(r"[,;:]+", " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip()

    return nome or "Livro"


def nome_epub(nome):
    return f"{limpar_nome(nome)} - Studio - Alma Scriptum.epub"


def nome_pdf(nome):
    return f"{limpar_nome(nome)} - PDF - Alma Scriptum.pdf"


def remover_sujeiras_texto(texto):
    if not texto:
        return texto

    padroes = [
        r"OceanofPDF\.com",
        r"OceanOfPDF\.com",
        r"OceanPDF\.com",
        r"oceanofpdf\.com",
        r"oceanofpdf",
        r"Ocean Of PDF",
        r"Ocean PDF",
        r"z-library\.sk",
        r"z-library",
        r"zlib",
        r"1lib\.sk",
        r"1lib",
        r"z-lib\.org",
        r"z-lib",
    ]

    for p in padroes:
        texto = re.sub(p, "", texto, flags=re.I)

    correcoes_fixas = {
        "deTODOS.Cada": "de TODOS. Cada",
        "deTODOS": "de TODOS",
        "TODOS.Cada": "TODOS. Cada",
        "paraaNola": "para a Nola",
        "paraaNo": "para a No",
        "daA Saga": "da Saga",
        "umaexperiência": "uma experiência",
        "completaincluindo": "completa incluindo",
        "sitewww": "site www",
        "meu sitewww": "meu site www",
    }

    for errado, certo in correcoes_fixas.items():
        texto = texto.replace(errado, certo)

    texto = re.sub(r"\bpara([aA])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+)", r"para \1 \2", texto)
    texto = re.sub(r"\bde([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ]{2,})", r"de \1", texto)
    texto = re.sub(r"\bdaA\s+", "da ", texto)
    texto = re.sub(r"\bdoO\s+", "do ", texto)

    texto = re.sub(
        r"([a-záàâãéêíóôõúç]{4,})(incluindo|experiência|personagem|história|série|saga|livro)",
        r"\1 \2",
        texto,
        flags=re.I
    )

    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"([,.!?;:])([A-Za-zÀ-ÿ])", r"\1 \2", texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


def texto_suspeito_para_gemini(texto):
    if not texto:
        return False

    t = texto.strip()

    if len(t) < 8:
        return False

    if re.search(r"[a-záàâãéêíóôõúç]{3,}[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}", t):
        return True

    suspeitas = [
        "deTODOS", "TODOS.Cada", "completaincluindo",
        "umaexperiência", "paraaNo", "passaEm",
        "deda", "dea", "doa",
    ]

    for item in suspeitas:
        if item.lower() in t.lower():
            return True

    if re.search(r"\b(the|and|with|your|you|she|he|they|this|that|was|were|have|from|into|would|could|should)\b", t, flags=re.I):
        return True

    return False


def gemini_revisar_trecho(texto):
    if not GEMINI_API_KEY or "COLE_SUA_CHAVE" in GEMINI_API_KEY:
        return remover_sujeiras_texto(texto)

    prompt = f"""
Revise APENAS o trecho abaixo em português brasileiro.

Regras obrigatórias:
- Não reescreva a história.
- Não mude nomes de personagens.
- Não traduza nomes próprios.
- Não adicione frases novas.
- Não remova conteúdo narrativo.
- Corrija apenas palavras grudadas, espaços, pontuação e pequenos erros visuais.
- Se houver trecho em inglês, traduza para português brasileiro.
- Retorne SOMENTE o texto corrigido, sem explicação.

Trecho:
{texto}
""".strip()

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "topP": 0.8,
            "maxOutputTokens": 1200,
        }
    }

    try:
        resposta = requests.post(url, json=payload, timeout=60)

        if resposta.status_code != 200:
            return remover_sujeiras_texto(texto)

        dados = resposta.json()
        candidatos = dados.get("candidates", [])

        if not candidatos:
            return remover_sujeiras_texto(texto)

        partes = candidatos[0].get("content", {}).get("parts", [])

        if not partes:
            return remover_sujeiras_texto(texto)

        revisado = partes[0].get("text", "").strip()

        if not revisado:
            return remover_sujeiras_texto(texto)

        return remover_sujeiras_texto(revisado)

    except Exception:
        return remover_sujeiras_texto(texto)


def corrigir_palavras_grudadas(texto):
    if not texto:
        return texto

    correcoes = {
        "deTODOS.Cada": "de TODOS. Cada",
        "deTODOS": "de TODOS",
        "TODOS.Cada": "TODOS. Cada",
        "paraaNola": "para a Nola",
        "aNola": "a Nola",
        "WitchesSériemas": "Witches Série, mas",
        "Sériemas": "Série, mas",
        "tambémpara": "também para",
        "relacionamentoscruciais": "relacionamentos cruciais",
        "daA Saga": "da Saga",
        "umaexperiência": "uma experiência",
        "cinematográfica completaincluindo": "cinematográfica completa incluindo",
        "completaincluindo": "completa incluindo",
        "sitewww": "site www",
        "meu sitewww": "meu site www",
        "uma experiência": "uma experiência",
        "completaincluindo": "completa incluindo",
        "completa incluindo": "completa incluindo",
        "cinematográficacompleta": "cinematográfica completa",
        "sitewww": "site www",
        "site www. smauggy. com": "site www.smauggy.com",
        "www. smauggy. com": "www.smauggy.com",
        "smauggy. com": "smauggy.com",
    }

    for errado, certo in correcoes.items():
        texto = texto.replace(errado, certo)

    texto = re.sub(
        r"([a-záàâãéêíóôõúç])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,})",
        r"\1 \2",
        texto
    )

    texto = re.sub(
        r"([.!?])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])",
        r"\1 \2",
        texto
    )

    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"\s+", " ", texto)
    texto = re.sub(r"\b(\w+)(incluindo)\b", r"\1 incluindo", texto, flags=re.I)
    texto = re.sub(r"\b(\w+)(experiência)\b", r"\1 experiência", texto, flags=re.I)
    texto = texto.replace("sitewww.", "site www.")
    texto = texto.replace("www. ", "www.")
    texto = texto.replace(". com", ".com")

    return texto.strip()


def revisar_html_simples(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["p", "div", "span"]):

        texto_original = tag.get_text(" ", strip=True)

        if not texto_original:
            continue

        texto_corrigido = remover_sujeiras_texto(texto_original)
        texto_corrigido = corrigir_palavras_grudadas(texto_corrigido)

        if texto_corrigido != texto_original:
            tag.clear()
            tag.append(texto_corrigido)

    return str(soup)


def revisar_html_gemini(html):
    soup = BeautifulSoup(html, "html.parser")
    corrigidos = 0

    for tag in soup.find_all(string=True):
        if tag.parent and tag.parent.name in ["script", "style", "code", "pre"]:
            continue

        original = str(tag)

        if not original.strip():
            continue

        limpo = remover_sujeiras_texto(original)

        if texto_suspeito_para_gemini(limpo):
            novo = gemini_revisar_trecho(limpo)
            time.sleep(0.7)
        else:
            novo = limpo

        if novo != original:
            tag.replace_with(novo)
            corrigidos += 1

    return str(soup), corrigidos


def revisar_epub(entrada, saida):
    book = epub.read_epub(str(entrada))

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            html = revisar_html_simples(html)
            item.set_content(html.encode("utf-8"))
        except Exception:
            pass

    epub.write_epub(str(saida), book)


def revisar_epub_com_gemini(entrada, saida, progresso_callback=None):
    book = epub.read_epub(str(entrada))
    docs = list(book.get_items_of_type(ITEM_DOCUMENT))
    total = len(docs) or 1
    total_corrigidos = 0

    for i, item in enumerate(docs, start=1):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            html, corrigidos = revisar_html_gemini(html)
            total_corrigidos += corrigidos
            item.set_content(html.encode("utf-8"))
        except Exception:
            pass

    epub.write_epub(str(saida), book)
    return total_corrigidos


def pegar_imagens_iniciais(caminho_epub, limite=3):
    book = epub.read_epub(str(caminho_epub))
    imagens = list(book.get_items_of_type(ITEM_IMAGE))

    escolhidas = []

    for img in imagens:
        nome = (img.file_name or "").lower()
        if "cover" in nome or "capa" in nome:
            escolhidas.append(img)

    for img in imagens:
        if img not in escolhidas:
            escolhidas.append(img)

    return escolhidas[:limite]


def salvar_imagem_temp(img):
    media = getattr(img, "media_type", "") or ""
    ext = ".jpg"

    if "png" in media:
        ext = ".png"
    elif "webp" in media:
        ext = ".webp"

    caminho = TEMP_DIR / f"imagem_{uuid.uuid4().hex}{ext}"

    with open(caminho, "wb") as f:
        f.write(img.get_content())

    return caminho


def ebook_convert_disponivel():
    return shutil.which("ebook-convert") is not None
    

def converter_com_calibre(entrada, saida):

    env = os.environ.copy()

    env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--no-sandbox "
        "--disable-gpu "
        "--disable-software-rasterizer "
        "--disable-dev-shm-usage"
    )

    env["QT_QUICK_BACKEND"] = "software"
    env["LIBGL_ALWAYS_SOFTWARE"] = "1"

    comando = [
    "ebook-convert",
    str(entrada),
    str(saida),

    "--paper-size", "a5",

    "--pdf-default-font-size", "14",

    "--disable-font-rescaling",

    "--chapter-mark", "none",

    "--page-breaks-before", "/",

    "--extra-css",
    "body{font-family:serif;}",
]

    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )

    if resultado.returncode != 0:
        raise Exception(
            resultado.stderr[-1000:]
            or resultado.stdout[-1000:]
            or "Erro na conversão."
        )


def limpar_sessao_capa(user_id):
    dados = usuarios.get(user_id, {})

    caminho = dados.get("capa_entrada")
    if caminho:
        try:
            Path(caminho).unlink(missing_ok=True)
        except Exception:
            pass

    dados.pop("capa_entrada", None)
    dados.pop("capa_imagens", None)
    dados.pop("capa_nome_original", None)
    dados.pop("imagem_escolhida", None)
    dados.pop("remover_imagens", None)


def remover_varias_imagens_epub(entrada, saida, nomes_imagens):
    book = epub.read_epub(str(entrada))

    nomes_limpos = [
        nome.replace("\\", "/").split("/")[-1]
        for nome in nomes_imagens
    ]

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            for img in soup.find_all("img"):
                src = img.get("src", "")
                src_limpo = src.replace("\\", "/").split("/")[-1]

                if src in nomes_imagens or src_limpo in nomes_limpos:
                    img.decompose()

            item.set_content(str(soup).encode("utf-8"))

        except Exception:
            pass

    novos_items = []

    for item in book.items:
        item_nome = getattr(item, "file_name", "")
        item_limpo = item_nome.replace("\\", "/").split("/")[-1]

        if item_nome not in nomes_imagens and item_limpo not in nomes_limpos:
            novos_items.append(item)

    book.items = novos_items
    epub.write_epub(str(saida), book)


def trocar_imagem_epub(entrada, saida, nome_imagem, nova_imagem_bytes):
    book = epub.read_epub(str(entrada))

    for item in book.get_items_of_type(ITEM_IMAGE):
        if item.file_name == nome_imagem:
            item.content = nova_imagem_bytes
            item.media_type = "image/jpeg"
            break

    epub.write_epub(str(saida), book)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not autorizado(user_id):
        await update.message.reply_text("⛔ Você não tem acesso ao Alma Scriptum Studio.")
        return

    usuarios[user_id] = {"modo": None}

    await update.message.reply_text(
        "📚 Alma Scriptum Studio\n\n"
        "Escolha o que deseja fazer:",
        reply_markup=painel_principal(),
    )


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if not autorizado(user_id):
        await query.message.reply_text("⛔ Acesso negado.")
        return

    if user_id not in usuarios:
        usuarios[user_id] = {"modo": None}

    data = query.data

    if data == "modo_revisar":
        usuarios[user_id]["modo"] = "revisar"
        await query.message.reply_text(
            "🛠 Modo Revisar / Limpar EPUB\n\n"
            "Envie o EPUB traduzido para eu limpar sujeiras de site e organizar o texto."
        )

    elif data == "modo_gemini":
        usuarios[user_id]["modo"] = "gemini"
        await query.message.reply_text(
            "🤖 Modo Revisar com Gemini\n\n"
            "Envie o EPUB já traduzido.\n"
            "Vou revisar apenas trechos suspeitos para economizar cota."
        )

    elif data == "modo_capa":
        usuarios[user_id]["modo"] = "capa"
        await query.message.reply_text(
            "🖼 Modo Editar capa\n\n"
            "Envie o EPUB. Eu vou mostrar apenas as primeiras imagens/capas iniciais."
        )

    elif data == "modo_conversor":
        await query.message.reply_text(
            "🔄 Conversor Alma Scriptum\n\n"
            "Escolha o tipo de conversão:",
            reply_markup=painel_conversor(),
        )

    elif data == "conv_epub_pdf":
        usuarios[user_id]["modo"] = "epub_pdf"
        await query.message.reply_text("📘 Envie o EPUB que deseja converter para PDF.")

    elif data == "conv_pdf_epub":
        usuarios[user_id]["modo"] = "pdf_epub"
        await query.message.reply_text("📄 Envie o PDF que deseja converter para EPUB.")

    elif data == "voltar":
        usuarios[user_id]["modo"] = None
        await query.message.reply_text(
            "📚 Alma Scriptum Studio\n\nEscolha uma opção:",
            reply_markup=painel_principal(),
        )

    elif data.startswith("remover_img_"):
        indice = int(data.replace("remover_img_", "")) - 1
        dados = usuarios.get(user_id, {})
        imagens = dados.get("capa_imagens", [])

        if indice < 0 or indice >= len(imagens):
            await query.message.reply_text("⚠️ Não encontrei essa imagem.")
            return

        if "remover_imagens" not in usuarios[user_id]:
            usuarios[user_id]["remover_imagens"] = []

        if indice not in usuarios[user_id]["remover_imagens"]:
            usuarios[user_id]["remover_imagens"].append(indice)

        await query.message.reply_text(
            f"🗑 Imagem {indice + 1} marcada para remoção.\n\n"
            "Quando terminar de escolher, aperte 📦 Finalizar edição."
        )

    elif data.startswith("trocar_img_"):
        indice = int(data.replace("trocar_img_", "")) - 1
        dados = usuarios.get(user_id, {})
        imagens = dados.get("capa_imagens", [])

        if indice < 0 or indice >= len(imagens):
            await query.message.reply_text("⚠️ Não encontrei essa imagem. Envie o EPUB novamente.")
            return

        usuarios[user_id]["modo"] = "aguardando_nova_capa"
        usuarios[user_id]["imagem_escolhida"] = indice

        await query.message.reply_text(
            "🔁 Envie agora a nova imagem da capa.\n\n"
            "Pode mandar como foto normal."
        )

    elif data == "manter_img":
        await query.message.reply_text("✅ Mantido. Nenhuma alteração feita nessa imagem.")

    elif data == "finalizar_capa":
        dados = usuarios.get(user_id, {})
        entrada = dados.get("capa_entrada")
        imagens = dados.get("capa_imagens", [])
        remover_indices = dados.get("remover_imagens", [])
        nome_original = dados.get("capa_nome_original", "Livro.epub")

        if not entrada:
            await query.message.reply_text("⚠️ Não encontrei o EPUB. Envie novamente.")
            return

        if not remover_indices:
            await query.message.reply_text("✅ Nenhuma imagem foi marcada para remover.")
            return

        saida = TEMP_DIR / nome_epub(nome_original)
        msg = await query.message.reply_text("📦 Finalizando edição de capa...")

        await atualizar_carregamento(
            msg,
            "🖼 Editor de capa",
            40,
            "🧹 Removendo imagens escolhidas...",
        )

        nomes_para_remover = [
            imagens[i]
            for i in remover_indices
            if 0 <= i < len(imagens)
        ]

        remover_varias_imagens_epub(entrada, saida, nomes_para_remover)

        await atualizar_carregamento(
            msg,
            "🖼 Editor de capa",
            85,
            "📦 Preparando EPUB atualizado...",
        )

        with open(saida, "rb") as f:
            await query.message.reply_document(
                document=InputFile(f, filename=nome_epub(nome_original)),
                caption="✅ Edição finalizada. EPUB atualizado.",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=90,
                pool_timeout=90,
            )

        await atualizar_carregamento(
            msg,
            "🖼 Editor de capa",
            100,
            "✅ EPUB editado e enviado.",
        )

        saida.unlink(missing_ok=True)
        limpar_sessao_capa(user_id)

    elif data == "cancelar":
        limpar_sessao_capa(user_id)
        usuarios[user_id] = {"modo": None}
        await query.message.reply_text("❌ Operação cancelada.")


async def receber_arquivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not autorizado(user_id):
        await update.message.reply_text("⛔ Você não tem acesso.")
        return

    modo = usuarios.get(user_id, {}).get("modo")

    if not modo:
        await update.message.reply_text("Escolha uma opção no painel primeiro. Use /start.")
        return

    documento = update.message.document

    if not documento:
        return

    nome_original = documento.file_name
    entrada = TEMP_DIR / f"{uuid.uuid4()}_{nome_original}"

    arquivo = await documento.get_file()
    await arquivo.download_to_drive(str(entrada))

    saida = None

    try:
        if modo == "revisar":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie apenas EPUB para revisão.")
                return

            msg = await update.message.reply_text("🛠 Preparando revisão...")
            await atualizar_carregamento(msg, "🛠 Revisando / Limpando EPUB", 15, "📥 Arquivo recebido. Preparando leitura...")

            saida = TEMP_DIR / nome_epub(nome_original)

            await atualizar_carregamento(msg, "🛠 Revisando / Limpando EPUB", 45, "🧹 Limpando sujeiras e organizando texto...")
            revisar_epub(entrada, saida)

            await atualizar_carregamento(msg, "🛠 Revisando / Limpando EPUB", 85, "📦 Preparando EPUB revisado para envio...")

            with open(saida, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=nome_epub(nome_original)),
                    caption="✅ EPUB revisado pelo Alma Scriptum Studio.",
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=90,
                    pool_timeout=90,
                )

            await atualizar_carregamento(msg, "🛠 Revisando / Limpando EPUB", 100, "✅ EPUB revisado e enviado.")

        elif modo == "gemini":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie apenas EPUB para revisão com Gemini.")
                return

            msg = await update.message.reply_text("🤖 Preparando revisão com Gemini...")
            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 10, "📥 EPUB recebido. Lendo estrutura...")

            saida = TEMP_DIR / nome_epub(nome_original)

            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 35, "🔍 Procurando trechos suspeitos...")
            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 55, "✨ Corrigindo com IA somente onde precisa...")

            corrigidos = revisar_epub_com_gemini(entrada, saida)

            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 85, "📦 Preparando EPUB revisado...")

            with open(saida, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=nome_epub(nome_original)),
                    caption=f"✅ Revisão com Gemini concluída.\n🧩 Trechos ajustados: {corrigidos}",
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=90,
                    pool_timeout=90,
                )

            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 100, "✅ EPUB revisado e enviado.")

        elif modo == "capa":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie apenas EPUB para editar capa.")
                return

            msg = await update.message.reply_text("🖼 Preparando editor de capa...")
            await atualizar_carregamento(msg, "🖼 Editor de capa", 20, "📥 EPUB recebido. Analisando início do livro...")

            imagens = pegar_imagens_iniciais(entrada, limite=3)

            usuarios[user_id]["capa_entrada"] = str(entrada)
            usuarios[user_id]["capa_nome_original"] = nome_original
            usuarios[user_id]["capa_imagens"] = [img.file_name for img in imagens]
            usuarios[user_id]["remover_imagens"] = []

            await atualizar_carregamento(msg, "🖼 Editor de capa", 70, "🖼 Separando capas/imagens iniciais...")

            if not imagens:
                await atualizar_carregamento(msg, "🖼 Editor de capa", 100, "⚠️ Não encontrei imagens no início do EPUB.")
                return

            for i, img in enumerate(imagens, start=1):
                img_path = salvar_imagem_temp(img)

                try:
                    with open(img_path, "rb") as img_file:
                        await update.message.reply_photo(
                            photo=img_file,
                            caption=f"🖼 Imagem inicial {i}\nArquivo interno: {img.file_name}",
                            reply_markup=InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton(f"🗑 Remover imagem {i}", callback_data=f"remover_img_{i}"),
                                    InlineKeyboardButton(f"🔁 Trocar imagem {i}", callback_data=f"trocar_img_{i}"),
                                ],
                                [
                                    InlineKeyboardButton("✅ Manter", callback_data="manter_img"),
                                    InlineKeyboardButton("📦 Finalizar edição", callback_data="finalizar_capa"),
                                ],
                            ]),
                        )
                except Exception as erro:
                    await update.message.reply_text(f"⚠️ Não consegui enviar a imagem {i}:\n{erro}")

                finally:
                    img_path.unlink(missing_ok=True)

            await atualizar_carregamento(
                msg,
                "🖼 Editor de capa",
                100,
                "✅ Imagens iniciais enviadas.\n\nAgora marque as imagens e aperte 📦 Finalizar edição.",
            )

            return

        elif modo == "epub_pdf":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie um arquivo EPUB.")
                return

            msg = await update.message.reply_text("🔄 Preparando conversão...")
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 15, "📥 EPUB recebido. Preparando Calibre...")

            saida = TEMP_DIR / nome_pdf(nome_original)

            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 45, "⚙️ Convertendo EPUB para PDF...")
            converter_com_calibre(entrada, saida)

            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 85, "📦 Preparando PDF para envio...")

            with open(saida, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=nome_pdf(nome_original)),
                    caption="✅ Conversão EPUB → PDF concluída.",
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=90,
                    pool_timeout=90,
                )

            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 100, "✅ Conversão concluída e enviada.")

        elif modo == "pdf_epub":
            if not nome_original.lower().endswith(".pdf"):
                await update.message.reply_text("⚠️ Envie um arquivo PDF.")
                return

            msg = await update.message.reply_text("🔄 Preparando conversão...")
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 15, "📥 PDF recebido. Preparando Calibre...")

            saida = TEMP_DIR / nome_epub(nome_original)

            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 45, "⚙️ Convertendo PDF para EPUB...")
            converter_com_calibre(entrada, saida)

            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 85, "📦 Preparando EPUB para envio...")

            with open(saida, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=nome_epub(nome_original)),
                    caption="✅ Conversão PDF → EPUB concluída.",
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=90,
                    pool_timeout=90,
                )

            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 100, "✅ Conversão concluída e enviada.")

    except (TimedOut, NetworkError):
        await update.message.reply_text("⚠️ O Telegram demorou responder. Se o arquivo apareceu acima, está tudo certo.")

    except Exception as erro:
        await update.message.reply_text(f"❌ Erro:\n{erro}")

    finally:
        try:
            if modo != "capa":
                entrada.unlink(missing_ok=True)

            if saida:
                saida.unlink(missing_ok=True)

        except Exception:
            pass


async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not autorizado(user_id):
        await update.message.reply_text("⛔ Você não tem acesso.")
        return

    modo = usuarios.get(user_id, {}).get("modo")

    if modo != "aguardando_nova_capa":
        await update.message.reply_text("⚠️ Escolha primeiro qual capa deseja trocar.")
        return

    dados = usuarios.get(user_id, {})
    entrada = dados.get("capa_entrada")
    imagens = dados.get("capa_imagens", [])
    indice = dados.get("imagem_escolhida")
    nome_original = dados.get("capa_nome_original", "Livro.epub")

    if not entrada or indice is None or indice < 0 or indice >= len(imagens):
        await update.message.reply_text("⚠️ Não encontrei o EPUB base. Envie novamente.")
        return

    nome_imagem = imagens[indice]

    foto = update.message.photo[-1]
    arquivo = await foto.get_file()

    nova_capa = TEMP_DIR / f"nova_capa_{uuid.uuid4().hex}.jpg"
    await arquivo.download_to_drive(str(nova_capa))

    saida = TEMP_DIR / nome_epub(nome_original)

    msg = await update.message.reply_text("🔁 Preparando troca de capa...")

    try:
        await atualizar_carregamento(msg, "🔁 Trocando capa", 40, "📥 Nova imagem recebida...")

        with open(nova_capa, "rb") as f:
            nova_bytes = f.read()

        await atualizar_carregamento(msg, "🔁 Trocando capa", 70, "🖼 Substituindo imagem escolhida...")

        trocar_imagem_epub(entrada, saida, nome_imagem, nova_bytes)

        await atualizar_carregamento(msg, "🔁 Trocando capa", 90, "📦 Preparando EPUB atualizado...")

        with open(saida, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=nome_epub(nome_original)),
                caption="✅ Capa trocada e EPUB atualizado.",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=90,
                pool_timeout=90,
            )

        await atualizar_carregamento(msg, "🔁 Trocando capa", 100, "✅ Capa trocada e enviada.")

    except Exception as erro:
        await update.message.reply_text(f"❌ Erro ao trocar capa:\n{erro}")

    finally:
        nova_capa.unlink(missing_ok=True)
        saida.unlink(missing_ok=True)
        limpar_sessao_capa(user_id)


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(180)
        .write_timeout(180)
        .connect_timeout(90)
        .pool_timeout(90)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    app.add_handler(MessageHandler(filters.Document.ALL, receber_arquivo))

    print("✅ Alma Scriptum Studio ONLINE")
    app.run_polling()


if __name__ == "__main__":
    main()
