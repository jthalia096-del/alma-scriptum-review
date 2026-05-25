import os
import re
import uuid
import shutil
import zipfile
import subprocess
import asyncio
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import TimedOut, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")

IDS_LIBERADOS = {8672397104, 1130170420}

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

usuarios = {}
cancelamentos = set()

FORMATOS_SAIDA = [
    "PDF", "DOCX", "TXT",
    "RTF", "MOBI", "AZW3",
    "LRF", "OEB", "PDB",
    "FB2", "RB", "EPUB",
    "HTMLZ", "KEPUB", "LIT",
    "PMLZ", "SNB", "TCR",
    "TXTZ", "ZIP",
]

FORMATOS_ENTRADA = {
    ".epub": "EPUB 📚 eBook",
    ".pdf": "PDF 📄 documento",
    ".mobi": "MOBI 📱 eBook",
    ".azw3": "AZW3 📚 Kindle",
    ".docx": "DOCX 📝 documento",
    ".txt": "TXT 📃 texto",
    ".rtf": "RTF 📄 texto",
    ".fb2": "FB2 📚 eBook",
    ".htmlz": "HTMLZ 🌐 eBook",
    ".kepub": "KEPUB 📘 Kobo",
    ".lit": "LIT 📚 eBook",
    ".lrf": "LRF 📚 eBook",
    ".pdb": "PDB 📚 eBook",
    ".pmlz": "PMLZ 📚 eBook",
    ".rb": "RB 📚 eBook",
    ".snb": "SNB 📚 eBook",
    ".tcr": "TCR 📚 eBook",
    ".txtz": "TXTZ 📃 texto",
    ".zip": "ZIP 📦 arquivo",
    ".kfx": "KFX 📚 Kindle",
    ".kfx-zip": "KFX-ZIP 📚 Kindle",
    ".oeb": "OEB 📚 eBook",
}


def autorizado(user_id):
    return user_id in IDS_LIBERADOS


def painel_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Conversor Alma Scriptum", callback_data="modo_conversor")],
        [InlineKeyboardButton("🖼 Editar capa", callback_data="modo_capa")],
        [InlineKeyboardButton("🛠 Limpar EPUB", callback_data="modo_revisar")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ])


def painel_formatos_saida(formato_entrada):
    botoes = []
    linha = []
    for fmt in FORMATOS_SAIDA:
        if fmt.lower() == formato_entrada.lower():
            continue
        linha.append(InlineKeyboardButton(fmt, callback_data=f"converter_para_{fmt.lower()}"))
        if len(linha) == 3:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="voltar")])
    return InlineKeyboardMarkup(botoes)


def barra_progresso(porcentagem):
    cheios = max(0, min(10, porcentagem // 10))
    return "🟩" * cheios + "⬜" * (10 - cheios)


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


def detectar_formato(nome):
    nome = nome or ""
    if nome.lower().endswith(".kfx-zip"):
        return "kfx-zip", FORMATOS_ENTRADA.get(".kfx-zip", "KFX-ZIP 📚 Kindle")
    ext = Path(nome).suffix.lower()
    return ext.replace(".", ""), FORMATOS_ENTRADA.get(ext, ext.replace(".", "").upper() or "DESCONHECIDO")


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


def nome_saida_convertido(nome_original, formato_saida):
    base = limpar_nome(nome_original)
    ext = formato_saida.lower()
    return f"{base} - {formato_saida.upper()} - Alma Scriptum.{ext}"


def nome_epub(nome):
    return f"{limpar_nome(nome)} - Studio - Alma Scriptum.epub"



async def converter_com_progresso(entrada, saida, formato_saida, msg, formato_entrada):
    tarefa = asyncio.create_task(asyncio.to_thread(rodar_calibre, entrada, saida, formato_saida))

    progresso = 45
    tempo_total = 0

    while not tarefa.done():
        await asyncio.sleep(20)
        tempo_total += 20

        if tarefa.done():
            break

        if progresso < 90:
            progresso += 3

        await atualizar_carregamento(
            msg,
            "🔄 Conversor Alma Scriptum",
            progresso,
            (
                f"⚙️ Convertendo {str(formato_entrada).upper()} para {str(formato_saida).upper()}...\\n\\n"
                f"⏳ Calibre ainda trabalhando há {tempo_total}s.\\n"
                "Arquivos grandes podem demorar bastante, principalmente EPUB → PDF."
            )
        )

    return await tarefa




def criar_soup_epub(html):
    """
    Parser leve para não travar EPUB grande no Railway.
    """
    return BeautifulSoup(html, "html.parser")


def texto_de_sujeira(texto):
    """
    Detecta SOMENTE sujeira conhecida.

    IMPORTANTE:
    Não marque qualquer http:// como sujeira, porque EPUB/XHTML usa URLs internas
    importantes em atributos como xmlns="http://www.w3.org/1999/xhtml".
    Foi isso que quebrava alguns livros.
    """
    if not texto:
        return False

    t = str(texto).strip()
    compact = re.sub(r"\s+", "", t).lower()

    padroes = [
        "oceanofpdf", "oceanpdf", "oceanofbooks",
        "z-library", "zlibrary", "z-lib", "1lib", "libgen",
        "annas-archive", "anna'sarchive", "anna’sarchive",
        "wattpad.com", "img.wattpad.com",
        "t.me/", "telegram.me/", "discord.gg",
        "uploadedby", "sharedby", "downloadedfrom",
        "freeebook", "ebookhunter", "bookfrom.net",
    ]

    if any(p in compact for p in padroes):
        return True

    # E-mail solto de divulgação/pirataria.
    if re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", t, flags=re.I):
        return True

    # Códigos gigantes aleatórios usados como marca d'água.
    if re.search(r"\b[a-z0-9]{60,}\b", compact, flags=re.I):
        return True

    return False


def limpar_texto_pesado(texto):
    """
    Limpa SOMENTE marcas conhecidas em texto visível.
    Não apaga URLs genéricas, para não destruir namespace XML do EPUB.
    """
    if not texto:
        return texto

    texto = str(texto)

    padroes = [
        r"Ocean\s*of\s*PDF\s*\.?\s*com",
        r"OceanofPDF\s*\.?\s*com",
        r"OceanPDF\s*\.?\s*com",
        r"OceanofPDF",
        r"Ocean\s*PDF",
        r"z\s*[\-_]?\s*library(?:\.(?:sk|org|to|net))?",
        r"z\s*[\-_]?\s*lib(?:\.(?:org|to|net))?",
        r"1lib(?:\.(?:sk|org|to|net))?",
        r"libgen(?:\.(?:is|rs|li))?",
        r"anna['’]?s\s*[\-_]?\s*archive",
        r"(?:https?://)?(?:www\.)?wattpad\.com/\S+",
        r"(?:https?://)?img\.wattpad\.com/\S+",
        r"(?:https?://)?(?:www\.)?oceanofpdf\.com\S*",
        r"(?:https?://)?(?:www\.)?z-library\.[^\s<]+",
        r"(?:https?://)?(?:www\.)?1lib\.[^\s<]+",
        r"(?:https?://)?t\.me/\S+",
        r"(?:https?://)?telegram\.me/\S+",
        r"(?:https?://)?discord\.gg/\S+",
        r"uploaded\s+by\s*:?.{0,80}",
        r"shared\s+by\s*:?.{0,80}",
        r"downloaded\s+from\s*:?.{0,80}",
        r"free\s+ebook\s+download.{0,80}",
    ]

    for p in padroes:
        texto = re.sub(p, "", texto, flags=re.I)

    texto = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "", texto)
    texto = re.sub(r"\b[A-Za-z0-9]{60,}\b", "", texto)
    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)

    return texto.strip()


def _tag_tem_conteudo_util(tag):
    if tag.find(["img", "image", "svg", "audio", "video", "object"]):
        return True
    return bool(tag.get_text(" ", strip=True))


def limpar_html_pesado(html):
    """
    Limpeza segura do capítulo XHTML/HTML.

    O segredo aqui é: limpar só TEXTO VISÍVEL e links suspeitos.
    Nunca limpar XML/OPF/NCX/CSS e nunca sair apagando qualquer URL,
    porque atributos como xmlns="http://www.w3.org/1999/xhtml" são obrigatórios.
    """
    soup = criar_soup_epub(html)

    # Remove scripts comuns. Não mexe em style para não quebrar CSS interno.
    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()

    # Links suspeitos: remove só o link ou o bloco quando for claramente propaganda.
    for tag in list(soup.find_all("a")):
        texto = tag.get_text(" ", strip=True)
        href = tag.get("href", "") or ""

        if texto_de_sujeira(href) or texto_de_sujeira(texto):
            if tag.find(["img", "image"]):
                # Se tem imagem dentro, mantém a imagem e tira só o clique/link.
                tag.unwrap()
            else:
                limpo = limpar_texto_pesado(texto)
                if limpo and not texto_de_sujeira(limpo):
                    tag.replace_with(NavigableString(limpo))
                else:
                    tag.decompose()

    # Remove blocos inteiros somente quando o TEXTO VISÍVEL for sujeira.
    # Não avalia atributos, para não destruir xmlns, href interno, classes etc.
    for tag in list(soup.find_all(["p", "div", "span", "font", "center", "small", "em", "i", "b", "strong", "li"])):
        if tag.find(["img", "image", "svg"]):
            continue

        texto = tag.get_text(" ", strip=True)
        if texto_de_sujeira(texto):
            texto_limpo = limpar_texto_pesado(texto)
            if texto_limpo and len(texto_limpo) > 2 and not texto_de_sujeira(texto_limpo):
                tag.clear()
                tag.append(NavigableString(texto_limpo))
            else:
                tag.decompose()

    # Limpa textos soltos, mas nunca dentro de style/script.
    for node in list(soup.find_all(string=True)):
        parent = getattr(node, "parent", None)
        parent_name = (getattr(parent, "name", "") or "").lower() if parent else ""

        if parent_name in ["script", "noscript", "style"]:
            continue

        original = str(node)
        if texto_de_sujeira(original):
            novo = limpar_texto_pesado(original)
            if novo and not texto_de_sujeira(novo):
                node.replace_with(NavigableString(novo))
            else:
                node.extract()

    # Remove tags vazias simples, preservando estrutura e mídia.
    for tag in list(soup.find_all(["p", "div", "span", "center", "font", "small", "li"])):
        if not _tag_tem_conteudo_util(tag):
            tag.decompose()

    return str(soup)


def escrever_epub_valido(saida, arquivos):
    """
    Escreve EPUB válido para leitores mais chatos:
    - mimetype precisa ser o primeiro arquivo;
    - mimetype precisa ficar SEM compressão;
    - demais arquivos podem ser comprimidos.
    """
    saida = Path(saida)

    with zipfile.ZipFile(saida, "w") as zout:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zout.writestr(info, arquivos.get("mimetype", b"application/epub+zip"))

        for nome, data in arquivos.items():
            nome_norm = str(nome).replace("\\", "/")
            if nome_norm == "mimetype":
                continue

            info = zipfile.ZipInfo(nome_norm)
            info.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(info, data)

    return saida


def limpar_nome_arquivo_interno(nome):
    return str(nome).replace("\\", "/")


def limpar_epub_rapido(entrada, saida):
    """
    Limpeza segura do EPUB.

    NÃO mexe em:
    - .opf
    - .ncx
    - .xml
    - .css
    - imagens
    - META-INF
    - mimetype

    Só limpa capítulos: .html, .xhtml e .htm.
    """
    alterados = 0
    arquivos = {}

    with zipfile.ZipFile(entrada, "r") as zin:
        for item in zin.infolist():
            nome_original = item.filename
            nome = limpar_nome_arquivo_interno(nome_original)
            nome_lower = nome.lower()
            data = zin.read(nome_original)

            if nome_lower.endswith((".html", ".xhtml", ".htm")):
                try:
                    # Mantém o máximo possível do arquivo original.
                    texto = data.decode("utf-8", errors="replace")
                    novo = limpar_html_pesado(texto)

                    if novo != texto:
                        alterados += 1
                        data = novo.encode("utf-8", errors="xmlcharrefreplace")

                except Exception:
                    # Se um capítulo der erro, mantém original em vez de quebrar o livro.
                    pass

            arquivos[nome] = data

    escrever_epub_valido(saida, arquivos)

    if not Path(saida).exists() or Path(saida).stat().st_size == 0:
        raise Exception("A limpeza terminou, mas o EPUB limpo não foi criado.")

    # Teste simples: garante que o EPUB final abre como ZIP e tem arquivos obrigatórios.
    with zipfile.ZipFile(saida, "r") as teste:
        nomes = set(teste.namelist())
        if "mimetype" not in nomes or "META-INF/container.xml" not in nomes:
            raise Exception("O EPUB limpo ficou sem estrutura obrigatória. Mantive a limpeza bloqueada para não enviar arquivo quebrado.")

    return alterados



def ebook_convert_disponivel():
    return shutil.which("ebook-convert") is not None


def limpar_epub_para_calibre(caminho_epub):
    caminho_epub = Path(caminho_epub)
    if caminho_epub.suffix.lower() != ".epub":
        return caminho_epub
    saida = TEMP_DIR / f"calibre_limpo_{uuid.uuid4().hex}.epub"
    try:
        with zipfile.ZipFile(caminho_epub, "r") as zin:
            with zipfile.ZipFile(saida, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    nome = item.filename.replace("\\", "/").lower()
                    if nome == "meta-inf/encryption.xml":
                        continue
                    zout.writestr(item, zin.read(item.filename))
        return saida
    except Exception:
        return caminho_epub


def ambiente_calibre():
    env = os.environ.copy()
    env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--no-sandbox --disable-gpu --disable-software-rasterizer"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_BACKEND"] = "software"
    env["QT_OPENGL"] = "software"
    env["QT_XCB_GL_INTEGRATION"] = "none"
    env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    env["MESA_LOADER_DRIVER_OVERRIDE"] = "llvmpipe"
    env["XDG_RUNTIME_DIR"] = str(TEMP_DIR)
    return env


def rodar_calibre(entrada, saida, formato_saida, timeout=3600):
    if not ebook_convert_disponivel():
        raise Exception("O comando ebook-convert do Calibre não foi encontrado.")

    entrada = Path(entrada)
    saida = Path(saida)

    entrada_convertida = limpar_epub_para_calibre(entrada) if entrada.suffix.lower() == ".epub" else entrada

    comando_base = ["ebook-convert", str(entrada_convertida), str(saida)]
    formato_saida = formato_saida.lower()

    if formato_saida == "pdf":
        comando_base += [
            "--paper-size", "a5",
            "--margin-left", "18",
            "--margin-right", "18",
            "--margin-top", "18",
            "--margin-bottom", "18",
            "--pdf-default-font-size", "14",
            "--disable-font-rescaling",
            "--chapter-mark", "none",
        ]

    elif formato_saida in ["epub", "mobi", "azw3", "fb2", "lit", "lrf", "pdb", "rb", "snb", "tcr", "txtz", "htmlz", "kepub"]:
        comando_base += [
            "--chapter-mark", "none",
        ]

    xvfb = shutil.which("xvfb-run")

    if xvfb:
        comando = [xvfb, "-a", "--server-args=-screen 0 1024x768x24"] + comando_base
    else:
        comando = comando_base

    try:
        resultado = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=ambiente_calibre(),
        )

    finally:
        try:
            if entrada_convertida != entrada and Path(entrada_convertida).exists():
                Path(entrada_convertida).unlink(missing_ok=True)
        except Exception:
            pass

    if resultado.returncode != 0:
        erro = resultado.stderr[-3000:] or resultado.stdout[-3000:] or "Falha na conversão."
        raise Exception(erro)

    if not saida.exists() or saida.stat().st_size == 0:
        raise Exception("O Calibre terminou, mas o arquivo convertido não foi criado.")

    return saida

def pegar_imagens_iniciais(caminho_epub, limite=3):
    book = epub.read_epub(str(caminho_epub))
    imagens = list(book.get_items_of_type(ITEM_IMAGE))
    escolhidas = [img for img in imagens if "cover" in (img.file_name or "").lower() or "capa" in (img.file_name or "").lower()]
    for img in imagens:
        if img not in escolhidas:
            escolhidas.append(img)
    return escolhidas[:limite]


def pegar_todas_imagens_epub(caminho_epub, limite=30):
    book = epub.read_epub(str(caminho_epub))
    imagens = list(book.get_items_of_type(ITEM_IMAGE))
    ordenadas = [img for img in imagens if "cover" in (img.file_name or "").lower() or "capa" in (img.file_name or "").lower()]
    for img in imagens:
        if img not in ordenadas:
            ordenadas.append(img)
    return ordenadas[:limite]


def salvar_imagem_temp(img):
    media = getattr(img, "media_type", "") or ""
    ext = ".png" if "png" in media else ".webp" if "webp" in media else ".jpg"
    caminho = TEMP_DIR / f"imagem_{uuid.uuid4().hex}{ext}"
    with open(caminho, "wb") as f:
        f.write(img.get_content())
    return caminho


def limpar_sessao_capa(user_id):
    dados = usuarios.get(user_id, {})
    for chave in ["capa_entrada", "conv_entrada"]:
        caminho = dados.get(chave)
        if caminho:
            try:
                Path(caminho).unlink(missing_ok=True)
            except Exception:
                pass
    for chave in ["capa_entrada", "capa_imagens", "capa_nome_original", "imagem_escolhida", "remover_imagens", "conv_entrada", "conv_nome_original", "conv_formato_entrada"]:
        dados.pop(chave, None)


def remover_varias_imagens_epub(entrada, saida, nomes_imagens):
    book = epub.read_epub(str(entrada))
    nomes_limpos = [nome.replace("\\", "/").split("/")[-1] for nome in nomes_imagens]
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            soup = criar_soup_epub(html)
            for img in soup.find_all("img"):
                src = img.get("src", "")
                src_limpo = src.replace("\\", "/").split("/")[-1]
                if src in nomes_imagens or src_limpo in nomes_limpos:
                    img.decompose()
            item.set_content(str(soup).encode("utf-8"))
        except Exception:
            pass
    book.items = [item for item in book.items if getattr(item, "file_name", "") not in nomes_imagens and getattr(item, "file_name", "").replace("\\", "/").split("/")[-1] not in nomes_limpos]
    epub.write_epub(str(saida), book)


def trocar_imagem_epub(entrada, saida, nome_imagem, nova_imagem_bytes):
    book = epub.read_epub(str(entrada))
    for item in book.get_items_of_type(ITEM_IMAGE):
        if item.file_name == nome_imagem:
            item.content = nova_imagem_bytes
            item.media_type = "image/jpeg"
            break
    epub.write_epub(str(saida), book)


def buscar_bytes_imagem_epub(entrada, nome_imagem):
    book = epub.read_epub(str(entrada))
    for item in book.get_items_of_type(ITEM_IMAGE):
        if item.file_name == nome_imagem:
            return item.get_content(), getattr(item, "media_type", "") or "image/jpeg"
    return None, None




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not autorizado(user_id):
        await update.message.reply_text("⛔ Você não tem acesso ao Alma Scriptum Studio.")
        return
    cancelamentos.add(user_id)
    usuarios[user_id] = {"modo": None}
    await update.message.reply_text("📚 Alma Scriptum Studio\n\nEscolha o que deseja fazer:", reply_markup=painel_principal())


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not autorizado(user_id):
        await query.message.reply_text("⛔ Acesso negado.")
        return
    usuarios.setdefault(user_id, {"modo": None})
    data = query.data
    if data == "modo_conversor":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "conversor_aguardando"
        await query.message.reply_text("🔄 Conversor Alma Scriptum\n\nEnvie o arquivo que deseja converter.\nEu detecto o formato e mostro as opções de saída.")
    elif data.startswith("converter_para_"):
        formato_saida = data.replace("converter_para_", "").lower()
        dados = usuarios.get(user_id, {})
        entrada = dados.get("conv_entrada")
        nome_original = dados.get("conv_nome_original")
        formato_entrada = dados.get("conv_formato_entrada", "arquivo")
        if not entrada or not Path(entrada).exists():
            await query.message.reply_text("⚠️ Não encontrei o arquivo. Envie novamente.")
            return
        if str(formato_entrada).lower() in ["kfx", "kfx-zip"]:
            await query.message.reply_text(
                "⚠️ Esse arquivo é KFX/KFX-ZIP do Kindle.\n\n"
                "O Calibre do Railway reconhece o nome, mas NÃO consegue converter KFX sem plugin próprio.\n"
                "Para converter, primeiro abra no Calibre do PC e converta para EPUB/AZW3. "
                "Depois envie o EPUB/AZW3 aqui no bot."
            )
            return

        msg = await query.message.reply_text("🔄 Preparando conversão...")
        try:
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 15, f"📥 Entrada: {formato_entrada.upper()}\n✨ Saída: {formato_saida.upper()}\n\nPreparando Calibre...")
            saida = TEMP_DIR / nome_saida_convertido(nome_original, formato_saida)
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 45, f"⚙️ Convertendo {formato_entrada.upper()} para {formato_saida.upper()}...\n\n⏳ Conversão iniciada. Aguarde o Calibre finalizar.")
            saida = await converter_com_progresso(entrada, saida, formato_saida, msg, formato_entrada)
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 85, "📦 Preparando arquivo convertido para envio...")
            with open(saida, "rb") as f:
                await query.message.reply_document(document=InputFile(f, filename=nome_saida_convertido(nome_original, formato_saida)), caption=f"✅ Conversão concluída: {formato_entrada.upper()} → {formato_saida.upper()}", read_timeout=600, write_timeout=600, connect_timeout=180, pool_timeout=180)
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 100, "✅ Conversão concluída e enviada.")
            try:
                Path(saida).unlink(missing_ok=True)
                Path(entrada).unlink(missing_ok=True)
            except Exception:
                pass
            limpar_sessao_capa(user_id)
            usuarios[user_id]["modo"] = "conversor_aguardando"
        except subprocess.TimeoutExpired:
            await query.message.reply_text("❌ Erro:\nO Calibre travou ou demorou mais de 10 minutos. Eu parei o processo para não deixar o bot preso.\n\nTente converter primeiro para EPUB/AZW3 no Calibre do PC ou use outro formato de saída.")
        except Exception as erro:
            await query.message.reply_text(f"❌ Erro:\n{erro}")
    elif data == "modo_capa":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "capa"
        await query.message.reply_text("🖼 Modo Editar capa\n\nEnvie o EPUB. Eu vou mostrar apenas as primeiras imagens/capas iniciais.")
    elif data == "modo_revisar":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "revisar"
        await query.message.reply_text(
            "🛠 Limpar EPUB\n\n"
            "Envie o EPUB para remover:\n"
            "• OceanofPDF\n"
            "• Wattpad links\n"
            "• z-library\n"
            "• URLs gigantes\n"
            "• sujeiras visuais"
        )

    elif data == "voltar":
        usuarios[user_id]["modo"] = None
        await query.message.reply_text("📚 Alma Scriptum Studio\n\nEscolha uma opção:", reply_markup=painel_principal())
    elif data.startswith("remover_img_"):
        indice = int(data.replace("remover_img_", "")) - 1
        imagens = usuarios.get(user_id, {}).get("capa_imagens", [])
        if indice < 0 or indice >= len(imagens):
            await query.message.reply_text("⚠️ Não encontrei essa imagem.")
            return
        usuarios[user_id].setdefault("remover_imagens", [])
        if indice not in usuarios[user_id]["remover_imagens"]:
            usuarios[user_id]["remover_imagens"].append(indice)
        await query.message.reply_text(f"🗑 Imagem {indice + 1} marcada para remoção.\n\nQuando terminar, aperte 📦 Finalizar edição.")
    elif data.startswith("trocar_img_"):
        indice = int(data.replace("trocar_img_", "")) - 1
        imagens = usuarios.get(user_id, {}).get("capa_imagens", [])
        if indice < 0 or indice >= len(imagens):
            await query.message.reply_text("⚠️ Não encontrei essa imagem. Envie o EPUB novamente.")
            return
        usuarios[user_id]["modo"] = "aguardando_nova_capa"
        usuarios[user_id]["imagem_escolhida"] = indice
        await query.message.reply_text("🔁 Envie agora a nova imagem.\n\nPode mandar como foto normal ou como arquivo de imagem.")
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
        msg = await query.message.reply_text("📦 Finalizando edição de imagem/capa...")
        nomes_para_remover = [imagens[i] for i in remover_indices if 0 <= i < len(imagens)]
        await atualizar_carregamento(msg, "🖼 Editor de capa", 45, "🧹 Removendo imagens escolhidas...")
        remover_varias_imagens_epub(entrada, saida, nomes_para_remover)
        await atualizar_carregamento(msg, "🖼 Editor de capa", 85, "📦 Preparando EPUB atualizado...")
        with open(saida, "rb") as f:
            await query.message.reply_document(document=InputFile(f, filename=nome_epub(nome_original)), caption="✅ Edição finalizada. EPUB atualizado.", read_timeout=600, write_timeout=600, connect_timeout=180, pool_timeout=180)
        await atualizar_carregamento(msg, "🖼 Editor de capa", 100, "✅ EPUB editado e enviado.")
        saida.unlink(missing_ok=True)
        limpar_sessao_capa(user_id)
    elif data == "cancelar":
        cancelamentos.add(user_id)
        limpar_sessao_capa(user_id)
        usuarios[user_id] = {"modo": None}
        await query.message.reply_text("❌ Operação cancelada.")


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not autorizado(user_id):
        return
    cancelamentos.add(user_id)
    limpar_sessao_capa(user_id)
    usuarios[user_id] = {"modo": None}
    await update.message.reply_text("❌ Operação cancelada. Use /start para abrir o painel novamente.")


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
    nome_original = documento.file_name or "arquivo"
    entrada = TEMP_DIR / f"{uuid.uuid4()}_{nome_original}"
    arquivo = await documento.get_file()
    await arquivo.download_to_drive(str(entrada))
    saida = None
    try:
        if modo == "conversor_aguardando":
            formato, descricao = detectar_formato(nome_original)
            if not formato or f".{formato.lower()}" not in FORMATOS_ENTRADA:
                await update.message.reply_text("⚠️ Formato não reconhecido. Envie EPUB, PDF, MOBI, AZW3, DOCX, TXT, RTF, FB2, HTMLZ, KEPUB, LIT, LRF, PDB, PMLZ, RB, SNB, TCR, TXTZ, ZIP, KFX, KFX-ZIP ou OEB.")
                entrada.unlink(missing_ok=True)
                return
            usuarios[user_id]["conv_entrada"] = str(entrada)
            usuarios[user_id]["conv_nome_original"] = nome_original
            usuarios[user_id]["conv_formato_entrada"] = formato
            await update.message.reply_text(f"📚 Alma Scriptum Converter\n\n📖 Arquivo detectado:\n{nome_original}\n\n✨ Tipo detectado: {descricao}\n🔄 Escolha o formato de saída:", reply_markup=painel_formatos_saida(formato))
            return
        if modo == "revisar":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie apenas EPUB.")
                return

            msg = await update.message.reply_text("🛠 Preparando limpeza...")
            saida = TEMP_DIR / nome_epub(nome_original)

            await atualizar_carregamento(
                msg,
                "🛠 Limpando EPUB",
                45,
                "🧹 Removendo links e sujeiras..."
            )

            alterados = await asyncio.to_thread(limpar_epub_rapido, entrada, saida)

            await atualizar_carregamento(
                msg,
                "🛠 Limpando EPUB",
                85,
                "📦 EPUB limpo criado. Preparando envio..."
            )

            tamanho_mb = Path(saida).stat().st_size / (1024 * 1024)

            await atualizar_carregamento(
                msg,
                "🛠 Limpando EPUB",
                90,
                f"📤 Enviando EPUB limpo...\n\nTamanho: {tamanho_mb:.1f} MB\nSe o arquivo for grande, essa parte pode demorar."
            )

            with open(saida, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=nome_epub(nome_original)),
                    caption=f"✅ EPUB limpo pelo Alma Scriptum.\n🧹 Capítulos ajustados: {alterados}\n📚 Estrutura OPF/NCX/XML/CSS preservada.",
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=180,
                    pool_timeout=180,
                )

            await atualizar_carregamento(
                msg,
                "🛠 Limpando EPUB",
                100,
                "✅ EPUB limpo enviado."
            )

        elif modo == "capa":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie apenas EPUB.")
                return
            titulo = "🖼 Editor de capa"
            msg = await update.message.reply_text("🖼 Preparando imagens...")
            limite = 3
            imagens = pegar_imagens_iniciais(entrada, limite=limite)
            usuarios[user_id]["capa_entrada"] = str(entrada)
            usuarios[user_id]["capa_nome_original"] = nome_original
            usuarios[user_id]["capa_imagens"] = [img.file_name for img in imagens]
            usuarios[user_id]["remover_imagens"] = []
            await atualizar_carregamento(msg, titulo, 70, f"🖼 Encontrei {len(imagens)} imagem(ns). Enviando prévias...")
            if not imagens:
                await atualizar_carregamento(msg, titulo, 100, "⚠️ Não encontrei imagens no EPUB.")
                return
            for i, img in enumerate(imagens, start=1):
                img_path = salvar_imagem_temp(img)
                try:
                    botoes = [[InlineKeyboardButton(f"🔁 Trocar imagem {i}", callback_data=f"trocar_img_{i}")], [InlineKeyboardButton(f"🗑 Remover imagem {i}", callback_data=f"remover_img_{i}"), InlineKeyboardButton("✅ Manter", callback_data="manter_img")], [InlineKeyboardButton("📦 Finalizar edição", callback_data="finalizar_capa")]]
                    with open(img_path, "rb") as img_file:
                        await update.message.reply_photo(photo=img_file, caption=f"🖼 Imagem {i}\nArquivo interno: {img.file_name}", reply_markup=InlineKeyboardMarkup(botoes))
                except Exception as erro:
                    await update.message.reply_text(f"⚠️ Não consegui enviar a imagem {i}:\n{erro}")
                finally:
                    img_path.unlink(missing_ok=True)
            await atualizar_carregamento(msg, titulo, 100, "✅ Imagens enviadas.")
            return
    except (TimedOut, NetworkError):
        await update.message.reply_text("⚠️ O Telegram demorou para enviar/responder. Se o arquivo não apareceu, tente enviar um EPUB menor ou rode a limpeza novamente.")
    except Exception as erro:
        await update.message.reply_text(f"❌ Erro:\n{erro}")
    finally:
        try:
            if modo not in ["capa", "conversor_aguardando"]:
                entrada.unlink(missing_ok=True)
            if saida:
                Path(saida).unlink(missing_ok=True)
        except Exception:
            pass


async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not autorizado(user_id):
        await update.message.reply_text("⛔ Você não tem acesso.")
        return
    modo = usuarios.get(user_id, {}).get("modo")
    if modo != "aguardando_nova_capa":
        await update.message.reply_text("⚠️ Escolha primeiro qual imagem deseja trocar.")
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
    msg = await update.message.reply_text("🔁 Preparando troca de imagem...")
    try:
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 40, "📥 Nova imagem recebida...")
        nova_bytes = nova_capa.read_bytes()
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 70, "🖼 Substituindo imagem escolhida...")
        trocar_imagem_epub(entrada, saida, nome_imagem, nova_bytes)
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 90, "📦 Preparando EPUB atualizado...")
        with open(saida, "rb") as f:
            await update.message.reply_document(document=InputFile(f, filename=nome_epub(nome_original)), caption="✅ Imagem trocada e EPUB atualizado.", read_timeout=600, write_timeout=600, connect_timeout=180, pool_timeout=180)
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 100, "✅ Imagem trocada e enviada.")
    except Exception as erro:
        await update.message.reply_text(f"❌ Erro ao trocar imagem:\n{erro}")
    finally:
        nova_capa.unlink(missing_ok=True)
        saida.unlink(missing_ok=True)
        limpar_sessao_capa(user_id)


async def receber_documento_imagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    modo = usuarios.get(user_id, {}).get("modo")
    if modo != "aguardando_nova_capa":
        return await receber_arquivo(update, context)
    documento = update.message.document
    if not documento:
        return
    mime = getattr(documento, "mime_type", "") or ""
    nome = documento.file_name or ""
    if not (mime.startswith("image/") or nome.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
        await update.message.reply_text("⚠️ Envie uma imagem para trocar.")
        return
    dados = usuarios.get(user_id, {})
    entrada = dados.get("capa_entrada")
    imagens = dados.get("capa_imagens", [])
    indice = dados.get("imagem_escolhida")
    nome_original = dados.get("capa_nome_original", "Livro.epub")
    if not entrada or indice is None or indice < 0 or indice >= len(imagens):
        await update.message.reply_text("⚠️ Não encontrei o EPUB base. Envie novamente.")
        return
    arquivo = await documento.get_file()
    nova_capa = TEMP_DIR / f"nova_imagem_{uuid.uuid4().hex}_{nome}"
    await arquivo.download_to_drive(str(nova_capa))
    saida = TEMP_DIR / nome_epub(nome_original)
    msg = await update.message.reply_text("🔁 Preparando troca de imagem...")
    try:
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 40, "📥 Nova imagem recebida...")
        nova_bytes = nova_capa.read_bytes()
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 70, "🖼 Substituindo imagem escolhida...")
        trocar_imagem_epub(entrada, saida, imagens[indice], nova_bytes)
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 90, "📦 Preparando EPUB atualizado...")
        with open(saida, "rb") as f:
            await update.message.reply_document(document=InputFile(f, filename=nome_epub(nome_original)), caption="✅ Imagem trocada e EPUB atualizado.", read_timeout=600, write_timeout=600, connect_timeout=180, pool_timeout=180)
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 100, "✅ Imagem trocada e enviada.")
    except Exception as erro:
        await update.message.reply_text(f"❌ Erro ao trocar imagem:\n{erro}")
    finally:
        nova_capa.unlink(missing_ok=True)
        saida.unlink(missing_ok=True)
        limpar_sessao_capa(user_id)


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(120)
        .pool_timeout(120)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    app.add_handler(MessageHandler(filters.Document.IMAGE, receber_documento_imagem))
    app.add_handler(MessageHandler(filters.Document.ALL, receber_arquivo))

    print("✅ Alma Scriptum Studio ONLINE — limpeza sem quebrar OPF/NCX")
    app.run_polling()

if __name__ == "__main__":
    main()
