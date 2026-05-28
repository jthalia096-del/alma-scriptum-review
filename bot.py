import os
import re
import uuid
import shutil
import zipfile
import subprocess
import asyncio
from html import unescape
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE

try:
    from weasyprint import HTML
except Exception:
    HTML = None

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
    entrada = Path(entrada)
    formato_saida = str(formato_saida).lower()

    if pode_usar_pdf_rapido(entrada, formato_saida):
        await atualizar_carregamento(
            msg,
            "🔄 Conversor Alma Scriptum",
            50,
            "⚡ Convertendo EPUB para PDF em modo rápido...\n\nSem usar o Calibre pesado."
        )

        resultado = await asyncio.to_thread(converter_epub_pdf_rapido, entrada, saida)

        await atualizar_carregamento(
            msg,
            "🔄 Conversor Alma Scriptum",
            85,
            "📦 PDF criado. Preparando envio..."
        )

        return resultado

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
                f"⚙️ Convertendo {str(formato_entrada).upper()} para {str(formato_saida).upper()}...\n\n"
                f"⏳ Calibre ainda trabalhando há {tempo_total}s.\n"
                "PDF de EPUB usa modo rápido. Outros formatos continuam no Calibre."
            )
        )

    return await tarefa



def criar_soup_epub(html):
    """
    Parser leve para não travar EPUB grande no Railway.
    """
    return BeautifulSoup(html, "html.parser")


def texto_de_sujeira(texto):
    if not texto:
        return False

    t = str(texto).strip()
    compact = re.sub(r"\s+", "", t).lower()

    padroes = [
        "oceanofpdf", "oceanpdf", "oceanofbooks",
        "z-library", "zlibrary", "z-lib", "1lib", "libgen",
        "wattpad.com", "img.wattpad.com",
        "annas-archive", "anna's archive", "vk.com",
        "t.me/", "telegram.me/", "discord.gg",
        "uploaded by", "shared by", "downloaded from",
        "free ebook", "ebook hunter", "bookfrom.net",
    ]

    if any(p in compact for p in padroes):
        return True

    if re.search(r"https?://", t, flags=re.I):
        return True

    if re.search(r"www\.", t, flags=re.I):
        return True

    if re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", t, flags=re.I):
        return True

    if re.search(r"[a-z0-9]{45,}", compact, flags=re.I):
        return True

    return False




def limpar_cabecalho_xhtml_malformado(html):
    """
    Corrige o erro que apareceu no seu print:
    alguns EPUBs vêm com o DTD/DOCTYPE quebrado assim:
    <?xml ...?>"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"<html...
    ou como texto visível:
    html PUBLIC "-//W3C//DTD XHTML 1.1//EN"

    Essa função remove esses pedaços ANTES do BeautifulSoup ler a página.
    """
    if not html:
        return html

    texto = unescape(str(html))

    # Remove URL do DTD que fica solta entre o XML e a tag <html>.
    texto = re.sub(
        r'(<\?xml[^>]*\?>)\s*["\']?https?://www\.w3\.org/TR/xhtml[^"\'<>\s]*["\']?\s*',
        r'\1\n',
        texto,
        flags=re.I,
    )

    # Remove qualquer URL DTD solta que tenha virado texto visível.
    texto = re.sub(
        r'["\']?https?://www\.w3\.org/TR/xhtml[^"\'<>\s]*["\']?',
        '',
        texto,
        flags=re.I,
    )

    # Remove fragmentos de PUBLIC/DTD quebrados, inclusive quando vêm sem <!DOCTYPE>.
    texto = re.sub(
        r'(?is)["\']?\s*html\s+PUBLIC\s+["\']?-//W3C//DTD\s+XHTML\s+1\.[01]//EN["\']?\s*',
        '',
        texto,
    )
    texto = re.sub(
        r'(?is)["\']?\s*PUBLIC\s+["\']?-//W3C//DTD\s+XHTML\s+1\.[01]//EN["\']?\s*',
        '',
        texto,
    )
    texto = re.sub(
        r'(?is)["\']?\s*-//W3C//DTD\s+XHTML\s+1\.[01]//EN["\']?\s*',
        '',
        texto,
    )
    texto = re.sub(
        r'(?is)["\']?\s*XHTML\s+1\.[01]//EN["\']?\s*',
        '',
        texto,
    )

    # Remove DOCTYPE quebrado completo, se existir.
    texto = re.sub(r'(?is)<!DOCTYPE[^>]*>', '', texto)

    # Caso comum: sobra aspas entre ?> e <html>.
    texto = re.sub(r'(\?>)\s*["\']+\s*(<html\b)', r'\1\n\2', texto, flags=re.I)

    return texto


def texto_lixo_html_quebrado(texto):
    """
    Detecta DOCTYPE/DTD/XML que vazou para dentro da página do livro.
    Exemplo do erro do print:
    html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
    """
    if not texto:
        return False

    t = unescape(str(texto)).strip()
    if not t:
        return False

    compact = re.sub(r"\s+", " ", t).strip().lower()

    padroes = [
        r"\bhtml\s+public\b",
        r"\bxhtml\s+1\.1\b",
        r"\bxhtml\s+1\.0\b",
        r"\b/w3c//dtd\b",
        r"www\.w3\.org/tr/xhtml",
        r"xhtml11/DTD/xhtml11\.dtd",
        r"\bdtd\s+xhtml\b",
        r"^-//w3c//dtd",
        r"^public\s+['\"]?-//w3c",
        r"^<!doctype\b",
        r"^<\?xml\b",
    ]

    return any(re.search(p, compact, flags=re.I) for p in padroes)


def limpar_lixo_html_quebrado_texto(texto):
    """
    Remove linhas/frases de cabeçalho HTML quebrado que aparecem como texto visível.
    Mantém o restante do parágrafo se tiver conteúdo real junto.
    """
    if not texto:
        return texto

    texto = unescape(str(texto))

    padroes = [
        r"(?im)^\s*html\s+PUBLIC\s+['\"]?-//W3C//DTD\s+XHTML\s+[^'\"]*['\"]?\s*$",
        r"(?im)^\s*PUBLIC\s+['\"]?-//W3C//DTD\s+XHTML\s+[^'\"]*['\"]?\s*$",
        r"(?im)^\s*XHTML\s+1\.[01]//EN['\"]?\s*$",
        r"(?im)^\s*['\"]?-//W3C//DTD\s+XHTML\s+[^'\"]*['\"]?\s*$",
        r"(?im)^\s*<!DOCTYPE[^>]*>\s*$",
        r"(?im)^\s*<\?xml[^>]*\?>\s*$",
    ]

    for p in padroes:
        texto = re.sub(p, "", texto, flags=re.I)

    # Remove pedaços quebrados que às vezes vêm na mesma linha, igual no print.
    texto = limpar_cabecalho_xhtml_malformado(texto)
    texto = re.sub(r"\bhtml\s+PUBLIC\s+['\"]?-//W3C//DTD\s+XHTML\s+1\.[01]//EN['\"]?", "", texto, flags=re.I)
    texto = re.sub(r"['\"]?-//W3C//DTD\s+XHTML\s+1\.[01]//EN['\"]?", "", texto, flags=re.I)
    texto = re.sub(r"\bXHTML\s+1\.[01]//EN['\"]?", "", texto, flags=re.I)
    texto = re.sub(r"['\"]?https?://www\.w3\.org/TR/xhtml[^'\"<>\s]*['\"]?", "", texto, flags=re.I)

    texto = re.sub(r"\s{2,}", " ", texto)
    return texto.strip()


def pagina_parece_so_imagem(soup):
    """
    Detecta páginas de capa/imagem para evitar que alguns apps deem zoom/corte.
    """
    body = soup.body if soup.body else soup
    imagens = body.find_all(["img", "image"])
    texto = body.get_text(" ", strip=True)

    texto_limpo = limpar_lixo_html_quebrado_texto(texto)
    texto_limpo = limpar_texto_pesado(texto_limpo) if texto_limpo else ""

    return len(imagens) >= 1 and len(texto_limpo.strip()) <= 40


def aplicar_css_imagens_epub(soup):
    """
    Deixa imagens/capas mais compatíveis entre apps:
    - usa contain, para mostrar a imagem inteira;
    - evita que o leitor interprete como cover/corte com zoom;
    - aplica só CSS seguro.
    """
    css = """
.alma-img-contain, img {
    max-width: 100% !important;
    height: auto !important;
    object-fit: contain !important;
}
html, body.alma-cover-page {
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
    min-height: 100% !important;
    text-align: center !important;
}
.alma-cover-page div, .alma-cover-page p {
    margin: 0 !important;
    padding: 0 !important;
    text-align: center !important;
}
.alma-cover-page img {
    display: block !important;
    margin: 0 auto !important;
    max-width: 100% !important;
    max-height: 100% !important;
    width: auto !important;
    height: auto !important;
    object-fit: contain !important;
}
"""

    if soup.head:
        style = soup.new_tag("style")
        style.string = css
        soup.head.append(style)

    if pagina_parece_so_imagem(soup):
        if soup.body:
            classes = soup.body.get("class", [])
            if isinstance(classes, str):
                classes = [classes]
            if "alma-cover-page" not in classes:
                classes.append("alma-cover-page")
            soup.body["class"] = classes

    for img in soup.find_all("img"):
        classes = img.get("class", [])
        if isinstance(classes, str):
            classes = [classes]
        if "alma-img-contain" not in classes:
            classes.append("alma-img-contain")
        img["class"] = classes

        # Remove atributos fixos que fazem alguns leitores Android cortar/zoomar imagem.
        for attr in ["width", "height"]:
            if attr in img.attrs:
                del img.attrs[attr]

        estilo = img.get("style", "")
        # Remove regras que costumam causar corte/zoom.
        estilo = re.sub(r"(?i)object-fit\s*:\s*cover\s*;?", "", estilo)
        estilo = re.sub(r"(?i)height\s*:\s*100%\s*;?", "", estilo)
        estilo = re.sub(r"(?i)width\s*:\s*100%\s*;?", "", estilo)
        estilo = re.sub(r"(?i)position\s*:\s*absolute\s*;?", "", estilo)

        extras = "max-width:100% !important; max-height:100% !important; width:auto !important; height:auto !important; object-fit:contain !important;"
        img["style"] = (estilo.strip() + "; " + extras).strip("; ")

    return soup



def limpar_texto_pesado(texto):
    if not texto:
        return texto

    texto = limpar_lixo_html_quebrado_texto(texto)
    texto = str(texto)

    padroes = [
        r"Ocean\s*of\s*PDF\.?\s*com",
        r"OceanofPDF\.?\s*com",
        r"OceanPDF\.?\s*com",
        r"OceanofPDF",
        r"Ocean\s*PDF",
        r"z[\s\-_]*library(?:\.sk|\.org)?",
        r"z[\s\-_]*lib(?:\.org)?",
        r"1lib(?:\.sk|\.org)?",
        r"libgen(?:\.is|\.rs)?",
        r"anna['’]?s[\s\-_]*archive",
        r"wattpad\.com/\S+",
        r"img\.wattpad\.com/\S+",
        r"https?://\S+",
        r"www\.\S+",
        r"t\.me/\S+",
        r"telegram\.me/\S+",
        r"discord\.gg/\S+",
        r"uploaded\s+by\s*:?\s*\S+",
        r"shared\s+by\s*:?\s*\S+",
        r"downloaded\s+from\s*:?\s*\S+",
    ]

    for p in padroes:
        texto = re.sub(p, "", texto, flags=re.I)

    texto = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "", texto)
    texto = re.sub(r"\b[A-Za-z0-9]{45,}\b", "", texto)
    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"\s{2,}", " ", texto)

    return texto.strip()


def limpar_html_pesado(html):
    """
    Limpeza pesada, mas segura:
    - remove OceanofPDF, z-library, Wattpad links e URLs gigantes;
    - NÃO remove imagens/personagens/capas internas;
    - não apaga bloco inteiro se ele tiver imagem;
    - preserva melhor a estrutura do EPUB.
    """
    html = limpar_cabecalho_xhtml_malformado(html)
    soup = criar_soup_epub(html)

    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()

    # Remove DOCTYPE/DTD/XML que vazou como texto visível dentro da página.
    for tag in list(soup.find_all(["p", "div", "span", "font", "center", "small", "em", "i", "b", "strong"])):
        texto = tag.get_text(" ", strip=True)
        if texto_lixo_html_quebrado(texto) and len(limpar_lixo_html_quebrado_texto(texto)) <= 5 and not tag.find(["img", "image"]):
            tag.decompose()

    # Limpa links <a>. Se tiver imagem dentro, preserva a imagem e remove só o link em volta.
    for tag in list(soup.find_all("a")):
        texto = tag.get_text(" ", strip=True)
        attrs = " ".join(str(v) for v in tag.attrs.values())

        if texto_de_sujeira(attrs) or texto_de_sujeira(texto):
            if tag.find(["img", "image"]):
                tag.unwrap()
            else:
                tag.decompose()

    # Limpa tags de texto sem apagar imagem.
    for tag in list(soup.find_all(["p", "div", "span", "font", "center", "small", "em", "i", "b", "strong"])):
        texto = tag.get_text(" ", strip=True)
        attrs = " ".join(str(v) for v in tag.attrs.values())
        tem_imagem = tag.find(["img", "image"]) is not None

        if texto_de_sujeira(attrs):
            if tem_imagem:
                for attr in list(tag.attrs.keys()):
                    val = str(tag.attrs.get(attr, ""))
                    if texto_de_sujeira(val):
                        del tag.attrs[attr]
            else:
                tag.decompose()
            continue

        if texto_de_sujeira(texto):
            texto_limpo = limpar_texto_pesado(texto)

            if tem_imagem:
                pass
            elif not texto_limpo or len(texto_limpo.strip()) <= 2:
                tag.decompose()
                continue

    # NÃO remove img/image por src do Wattpad. Remove só source suspeito.
    for tag in list(soup.find_all(["source"])):
        attrs = " ".join(str(v) for v in tag.attrs.values())
        if texto_de_sujeira(attrs):
            tag.decompose()

    # Limpa textos soltos.
    for node in list(soup.find_all(string=True)):
        parent = getattr(node, "parent", None)
        parent_name = getattr(parent, "name", "") if parent else ""

        if parent_name in ["script", "noscript"]:
            continue

        original = str(node)

        if texto_lixo_html_quebrado(original):
            novo = limpar_lixo_html_quebrado_texto(original)
            if novo.strip():
                node.replace_with(NavigableString(novo))
            else:
                node.extract()
            continue

        if parent_name in ["style"]:
            novo_css = limpar_texto_pesado(original)
            node.replace_with(NavigableString(novo_css))
            continue

        if texto_de_sujeira(original):
            novo = limpar_texto_pesado(original)

            if novo.strip():
                node.replace_with(NavigableString(novo))
            else:
                node.extract()

            continue

        novo = limpar_texto_pesado(original)

        if novo != original:
            if novo.strip():
                node.replace_with(NavigableString(novo))
            else:
                node.extract()

    # Remove tags vazias, mas nunca se tiver imagem.
    for tag in list(soup.find_all(["p", "div", "span", "center", "font", "small"])):
        if tag.find(["img", "image"]):
            continue
        if not tag.get_text(" ", strip=True):
            tag.decompose()

    soup = aplicar_css_imagens_epub(soup)

    resultado = str(soup)
    resultado = limpar_cabecalho_xhtml_malformado(resultado)
    return resultado



def escrever_epub_valido(saida, arquivos):
    """
    Escreve EPUB válido para leitores mais chatos:
    - mimetype precisa ser o primeiro arquivo;
    - mimetype precisa ficar SEM compressão;
    - demais arquivos podem ser comprimidos.
    """
    saida = Path(saida)

    with zipfile.ZipFile(saida, "w") as zout:
        if "mimetype" in arquivos:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zout.writestr(info, arquivos["mimetype"])
        else:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zout.writestr(info, b"application/epub+zip")

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
    Limpa EPUB sem quebrar imagens e sem quebrar OPF/NCX/XML.
    IMPORTANTE:
    Não limpar .opf/.ncx/.xml com regex de URL, porque esses arquivos têm
    namespaces obrigatórios com http://. Se remover, alguns apps dão erro de decodificação.
    """
    alterados = 0
    arquivos = {}

    with zipfile.ZipFile(entrada, "r") as zin:
        for item in zin.infolist():
            nome_original = item.filename
            nome = limpar_nome_arquivo_interno(nome_original)
            nome_lower = nome.lower()

            data = zin.read(nome_original)

            if nome_lower == "meta-inf/encryption.xml":
                alterados += 1
                continue

            # Só limpa capítulos HTML. Preserva imagens e estrutura.
            if nome_lower.endswith((".html", ".xhtml", ".htm")):
                try:
                    texto = data.decode("utf-8", errors="ignore")
                    novo = limpar_html_pesado(texto)

                    if novo != texto:
                        alterados += 1
                        data = novo.encode("utf-8", errors="xmlcharrefreplace")

                except Exception:
                    pass

            # CSS pode ser limpo, mas sem destruir o EPUB se der erro.
            elif nome_lower.endswith(".css"):
                try:
                    texto = data.decode("utf-8", errors="ignore")
                    novo = limpar_texto_pesado(texto)

                    if novo != texto:
                        alterados += 1
                        data = novo.encode("utf-8", errors="xmlcharrefreplace")

                except Exception:
                    pass

            # .opf/.ncx/.xml ficam intactos para manter compatibilidade.
            arquivos[nome] = data

    escrever_epub_valido(saida, arquivos)

    if not Path(saida).exists() or Path(saida).stat().st_size == 0:
        raise Exception("A limpeza terminou, mas o EPUB limpo não foi criado.")

    return alterados







def obter_htmls_do_spine_epub(pasta_epub):
    """
    Lê o OPF e devolve os capítulos na ordem REAL do spine.
    Isso evita o erro de imagem/capa ir parar no final porque o código antigo
    ordenava os XHTML pelo nome do arquivo.
    """
    pasta_epub = Path(pasta_epub)

    container = pasta_epub / "META-INF" / "container.xml"
    opf_path = None

    try:
        if container.exists():
            texto_container = container.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', texto_container, flags=re.I)
            if m:
                opf_path = pasta_epub / m.group(1).replace("\\", "/")
    except Exception:
        opf_path = None

    if not opf_path or not opf_path.exists():
        encontrados = list(pasta_epub.rglob("*.opf"))
        opf_path = encontrados[0] if encontrados else None

    if not opf_path or not opf_path.exists():
        return []

    opf = opf_path.read_text(encoding="utf-8", errors="ignore")
    base = opf_path.parent

    manifest = {}
    for attrs in re.findall(r'<item\b([^>]+)>', opf, flags=re.I):
        id_m = re.search(r'\bid=["\']([^"\']+)["\']', attrs, flags=re.I)
        href_m = re.search(r'\bhref=["\']([^"\']+)["\']', attrs, flags=re.I)
        media_m = re.search(r'\bmedia-type=["\']([^"\']+)["\']', attrs, flags=re.I)
        if id_m and href_m:
            item_id = id_m.group(1)
            href = href_m.group(1).replace("\\", "/")
            media = media_m.group(1).lower() if media_m else ""
            manifest[item_id] = (base / href).resolve(), media

    ordem = []
    vistos = set()
    for attrs in re.findall(r'<itemref\b([^>]+)>', opf, flags=re.I):
        idref_m = re.search(r'\bidref=["\']([^"\']+)["\']', attrs, flags=re.I)
        if not idref_m:
            continue
        item_id = idref_m.group(1)
        caminho, media = manifest.get(item_id, (None, ""))
        if not caminho or not caminho.exists():
            continue
        if not (str(caminho).lower().endswith((".xhtml", ".html", ".htm")) or "html" in media):
            continue
        nome = caminho.name.lower()
        if nome in ["nav.xhtml", "toc.xhtml"]:
            continue
        key = str(caminho)
        if key not in vistos:
            ordem.append(caminho)
            vistos.add(key)

    return ordem


def extrair_htmls_epub_ordenado(caminho_epub):
    """
    Extrai HTML/XHTML do EPUB para conversão rápida em PDF.
    Agora usa a ordem do OPF/spine, não ordem alfabética.
    Isso impede capa/personagens/imagens de irem para o final.
    """
    caminho_epub = Path(caminho_epub)
    pasta = TEMP_DIR / f"epub_pdf_{uuid.uuid4().hex}"
    pasta.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(caminho_epub, "r") as zin:
        zin.extractall(pasta)

    htmls = obter_htmls_do_spine_epub(pasta)

    # Fallback antigo, só se o EPUB não tiver spine legível.
    if not htmls:
        for p in pasta.rglob("*"):
            if p.suffix.lower() in [".xhtml", ".html", ".htm"]:
                nome = p.name.lower()
                if nome in ["nav.xhtml", "toc.xhtml"]:
                    continue
                htmls.append(p)
        htmls = sorted(htmls, key=lambda x: str(x).lower())

    return pasta, htmls


def preparar_html_para_pdf(conteudo, arquivo_base):
    soup = BeautifulSoup(conteudo, "html.parser")

    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()

    # Preserva imagens e ajusta caminhos relativos.
    # Inclui img normal e image de SVG, porque muitos EPUBs usam <svg><image href="..."></svg>.
    for img in soup.find_all(["img", "image"]):
        src = img.get("src") or img.get("href") or img.get("xlink:href") or ""
        if src and not src.startswith(("http://", "https://", "data:", "file://")):
            img_path = (arquivo_base.parent / src).resolve()
            if img_path.exists():
                uri = img_path.as_uri()
                if img.name == "img":
                    img["src"] = uri
                else:
                    if img.get("href") is not None:
                        img["href"] = uri
                    if img.get("xlink:href") is not None:
                        img["xlink:href"] = uri
                    if img.get("href") is None and img.get("xlink:href") is None:
                        img["href"] = uri

    body = soup.body if soup.body else soup
    return str(body)


def converter_epub_pdf_rapido(entrada, saida):
    """
    Conversão rápida EPUB -> PDF sem usar Calibre.
    Evita travamento do Calibre no Railway.
    """
    if HTML is None:
        raise Exception(
            "WeasyPrint não está instalado. Adicione 'weasyprint' no requirements.txt "
            "ou use outro formato de saída."
        )

    pasta = None

    try:
        pasta, htmls = extrair_htmls_epub_ordenado(entrada)

        if not htmls:
            raise Exception("Não encontrei capítulos HTML dentro do EPUB.")

        partes = []

        for h in htmls:
            try:
                conteudo = h.read_text(encoding="utf-8", errors="ignore")
                partes.append(preparar_html_para_pdf(conteudo, h))
            except Exception:
                pass

        if not partes:
            raise Exception("Não consegui preparar o conteúdo do EPUB para PDF.")

        html_final = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
@page { size: A5; margin: 16mm; }
body {
    font-family: serif;
    font-size: 13.5pt;
    line-height: 1.35;
    text-align: justify;
}
h1, h2, h3, h4 {
    text-align: center;
    page-break-before: always;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}
p { margin: 0 0 .75em 0; }
.chapter-break { page-break-after: always; }
</style>
</head>
<body>
""" + "\n<div class='chapter-break'></div>\n".join(partes) + """
</body>
</html>"""

        HTML(string=html_final, base_url=str(pasta)).write_pdf(str(saida))

        if not Path(saida).exists() or Path(saida).stat().st_size == 0:
            raise Exception("O PDF rápido não foi criado.")

        return Path(saida)

    finally:
        if pasta:
            shutil.rmtree(pasta, ignore_errors=True)


def pode_usar_pdf_rapido(entrada, formato_saida):
    return Path(entrada).suffix.lower() == ".epub" and str(formato_saida).lower() == "pdf"


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



def escrever_zip_epub_preservando_mimetype(saida, arquivos):
    """
    Recria EPUB sem quebrar Kindle:
    - mimetype primeiro e sem compressão;
    - imagens ficam intocadas;
    - OPF/NCX/XML ficam intocados, salvo quando função específica mexer.
    """
    saida = Path(saida)
    with zipfile.ZipFile(saida, "w") as zout:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zout.writestr(info, arquivos.get("mimetype", b"application/epub+zip"))

        for nome, data in arquivos.items():
            nome = str(nome).replace("\\", "/")
            if nome == "mimetype":
                continue
            info = zipfile.ZipInfo(nome)
            info.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(info, data)

    return saida


def limpar_html_minimo_para_kindle(html):
    """
    Limpeza mínima para conversão Kindle.
    Importante: NÃO reescreve toda a página com BeautifulSoup e NÃO mexe em src de imagem.
    Isso evita capa/imagens sumirem no Kindle.
    """
    if not html:
        return html

    texto = str(html)
    texto = limpar_cabecalho_xhtml_malformado(texto)

    # Remove só lixo de cabeçalho que virou texto visível.
    texto = re.sub(r"(?im)^\s*html\s+PUBLIC\s+['\"]?-//W3C//DTD\s+XHTML\s+[^'\"]*['\"]?\s*$", "", texto)
    texto = re.sub(r"(?im)^\s*PUBLIC\s+['\"]?-//W3C//DTD\s+XHTML\s+[^'\"]*['\"]?\s*$", "", texto)
    texto = re.sub(r"(?im)^\s*XHTML\s+1\.[01]//EN['\"]?\s*$", "", texto)
    texto = re.sub(r"(?im)^\s*['\"]?-//W3C//DTD\s+XHTML\s+[^'\"]*['\"]?\s*$", "", texto)

    # Remove links/textos sujos, sem tocar caminhos locais de imagens.
    for p in [
        r"Ocean\s*of\s*PDF\.?\s*com",
        r"OceanofPDF\.?\s*com",
        r"OceanPDF\.?\s*com",
        r"z[\s\-_]*library(?:\.sk|\.org)?",
        r"z[\s\-_]*lib(?:\.org)?",
        r"1lib(?:\.sk|\.org)?",
        r"anna['’]?s[\s\-_]*archive",
        r"uploaded\s+by\s*:?\s*\S+",
        r"shared\s+by\s*:?\s*\S+",
        r"downloaded\s+from\s*:?\s*\S+",
    ]:
        texto = re.sub(p, "", texto, flags=re.I)

    return texto


def encontrar_opf_no_epub(zin):
    """Acha o caminho do .opf pelo META-INF/container.xml."""
    try:
        container = zin.read("META-INF/container.xml").decode("utf-8", errors="ignore")
        m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', container, flags=re.I)
        if m:
            return m.group(1).replace("\\", "/")
    except Exception:
        pass

    for nome in zin.namelist():
        if nome.lower().endswith(".opf"):
            return nome.replace("\\", "/")
    return None


def normalizar_relativo(base, href):
    base = str(base).replace("\\", "/")
    href = str(href).replace("\\", "/")
    if "/" in base:
        pasta = base.rsplit("/", 1)[0]
        return str(Path(pasta) / href).replace("\\", "/")
    return href


def extrair_capa_do_epub_para_calibre(caminho_epub):
    """
    Extrai a imagem de capa para passar ao ebook-convert com --cover.
    Isso ajuda o Kindle a mostrar capa na biblioteca e no começo do livro.
    """
    caminho_epub = Path(caminho_epub)
    try:
        with zipfile.ZipFile(caminho_epub, "r") as zin:
            opf_path = encontrar_opf_no_epub(zin)
            candidatos = []

            if opf_path:
                opf = zin.read(opf_path).decode("utf-8", errors="ignore")

                # EPUB 2: <meta name="cover" content="cover-id" />
                m = re.search(r'<meta[^>]+name=["\']cover["\'][^>]+content=["\']([^"\']+)["\']', opf, flags=re.I)
                cover_id = m.group(1) if m else None

                items = re.findall(r'<item\b([^>]+)>', opf, flags=re.I)
                for attrs in items:
                    id_m = re.search(r'\bid=["\']([^"\']+)["\']', attrs, flags=re.I)
                    href_m = re.search(r'\bhref=["\']([^"\']+)["\']', attrs, flags=re.I)
                    media_m = re.search(r'\bmedia-type=["\']([^"\']+)["\']', attrs, flags=re.I)
                    prop_m = re.search(r'\bproperties=["\']([^"\']+)["\']', attrs, flags=re.I)

                    if not href_m:
                        continue

                    item_id = id_m.group(1) if id_m else ""
                    href = href_m.group(1)
                    media = media_m.group(1).lower() if media_m else ""
                    props = prop_m.group(1).lower() if prop_m else ""

                    if not media.startswith("image/"):
                        continue

                    caminho = normalizar_relativo(opf_path, href)

                    score = 0
                    nome = caminho.lower()
                    if cover_id and item_id == cover_id:
                        score += 100
                    if "cover-image" in props:
                        score += 100
                    if "cover" in nome or "capa" in nome:
                        score += 50
                    if nome.endswith((".jpg", ".jpeg", ".png", ".webp")):
                        score += 5
                    candidatos.append((score, caminho, media))

            # fallback: procura imagem com cover/capa no nome
            for nome in zin.namelist():
                low = nome.lower()
                if low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    score = 10
                    if "cover" in low or "capa" in low:
                        score += 60
                    candidatos.append((score, nome, "image/jpeg"))

            candidatos = sorted(candidatos, reverse=True)
            for _score, nome_img, media in candidatos:
                try:
                    data = zin.read(nome_img)
                    ext = Path(nome_img).suffix.lower()
                    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                        ext = ".jpg"
                    saida = TEMP_DIR / f"capa_kindle_{uuid.uuid4().hex}{ext}"
                    saida.write_bytes(data)
                    return saida
                except Exception:
                    continue
    except Exception:
        pass

    return None


def preparar_epub_para_kindle(caminho_epub):
    """
    Cria uma cópia segura para converter em AZW3/MOBI.
    Não remove imagens do manifest, não altera OPF/NCX e não usa limpeza agressiva.
    Também evita página fantasma de imagem no final causada por reescrita pesada de XHTML.
    """
    caminho_epub = Path(caminho_epub)
    if caminho_epub.suffix.lower() != ".epub":
        return caminho_epub

    saida = TEMP_DIR / f"kindle_seguro_{uuid.uuid4().hex}.epub"
    arquivos = {}

    with zipfile.ZipFile(caminho_epub, "r") as zin:
        for item in zin.infolist():
            nome_original = item.filename
            nome = nome_original.replace("\\", "/")
            nome_lower = nome.lower()

            if nome_lower == "meta-inf/encryption.xml":
                continue

            data = zin.read(nome_original)

            # Para Kindle: limpeza mínima nos XHTML/HTML. Não mexe em imagens, OPF, NCX, XML e CSS.
            if nome_lower.endswith((".html", ".xhtml", ".htm")):
                try:
                    html = data.decode("utf-8", errors="ignore")
                    novo = limpar_html_minimo_para_kindle(html)
                    data = novo.encode("utf-8", errors="xmlcharrefreplace")
                except Exception:
                    pass

            arquivos[nome] = data

    escrever_zip_epub_preservando_mimetype(saida, arquivos)
    return saida


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

    formato_saida = formato_saida.lower()
    kindle_saida = formato_saida in ["azw3", "mobi"]
    capa_temp = None

    if entrada.suffix.lower() == ".epub" and kindle_saida:
        entrada_convertida = preparar_epub_para_kindle(entrada)
        capa_temp = extrair_capa_do_epub_para_calibre(entrada_convertida)
    elif entrada.suffix.lower() == ".epub":
        entrada_convertida = limpar_epub_para_calibre(entrada)
    else:
        entrada_convertida = entrada

    comando_base = ["ebook-convert", str(entrada_convertida), str(saida)]

    if capa_temp and Path(capa_temp).exists():
        comando_base += ["--cover", str(capa_temp)]

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

        if formato_saida in ["azw3", "mobi"]:
            comando_base += [
                "--preserve-cover-aspect-ratio",
                "--prefer-metadata-cover",
                "--disable-font-rescaling",
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
            if capa_temp and Path(capa_temp).exists():
                Path(capa_temp).unlink(missing_ok=True)
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
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 15, f"📥 Entrada: {formato_entrada.upper()}\n✨ Saída: {formato_saida.upper()}\n\nPreparando conversão...")
            saida = TEMP_DIR / nome_saida_convertido(nome_original, formato_saida)
            await atualizar_carregamento(msg, "🔄 Conversor Alma Scriptum", 45, f"⚙️ Convertendo {formato_entrada.upper()} para {formato_saida.upper()}...\n\n⏳ Se for EPUB → PDF, uso modo rápido. Nos outros formatos, uso Calibre.")
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
                    caption=f"✅ EPUB limpo e compatível pelo Alma Scriptum.\n🧹 Arquivos internos ajustados: {alterados}\n📚 Estrutura OPF/NCX preservada.",
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

    print("✅ Alma Scriptum Studio ONLINE — limpeza compatível + PDF rápido")
    app.run_polling()

if __name__ == "__main__":
    main()
