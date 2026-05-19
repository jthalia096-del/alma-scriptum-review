import os
import re
import uuid
import time
import json
import shutil
import zipfile
import subprocess
import asyncio
from io import BytesIO
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageFilter = None
    ImageOps = None

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

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
MAX_GEMINI_TRECHOS = int(os.getenv("MAX_GEMINI_TRECHOS", "6"))
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "10"))

IDS_LIBERADOS = {
    8672397104,
    1130170420,
}

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

usuarios = {}
cancelamentos = set()


def autorizado(user_id):
    return user_id in IDS_LIBERADOS


def painel_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 Revisar / Limpar EPUB", callback_data="modo_revisar")],
        [InlineKeyboardButton("🤖 Revisar com Gemini", callback_data="modo_gemini_menu")],
        [InlineKeyboardButton("🖼 Traduzir / trocar imagens", callback_data="modo_imagens")],
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


def painel_gemini():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Revisão leve", callback_data="gemini_leve")],
        [InlineKeyboardButton("🟡 Revisão média", callback_data="gemini_media")],
        [InlineKeyboardButton("🔴 Revisão pesada", callback_data="gemini_pesada")],
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


def limpar_texto_inteligente(texto):
    """
    Limpeza pesada, mas segura:
    - remove sites;
    - junta palavras quebradas por hífen/soft-hyphen;
    - corrige palavras grudadas comuns;
    - corrige pedaços quebrados tipo 'lágri mas';
    - corrige letras sobrando no começo tipo 'TO grito' -> 'O grito'.
    """
    if not texto:
        return texto

    texto = str(texto)
    texto = texto.replace("\u00ad", "")  # soft hyphen invisível
    texto = texto.replace("‐", "-").replace("‑", "-").replace("–", "—")

    # Remove marcas de sites
    padroes_sites = [
        r"OceanofPDF\.com", r"OceanOfPDF\.com", r"OceanPDF\.com",
        r"oceanofpdf\.com", r"oceanofpdf", r"Ocean Of PDF", r"Ocean PDF",
        r"z-library\.sk", r"z-library", r"zlib", r"1lib\.sk", r"1lib",
        r"z-lib\.org", r"z-lib",
    ]

    for p in padroes_sites:
        texto = re.sub(p, "", texto, flags=re.I)

    # Junta hifenização falsa de quebra de linha: protagonis- ta -> protagonista
    texto = re.sub(
        r"([A-Za-zÀ-ÿ]{2,})-\s+([a-záàâãéêíóôõúç]{2,})",
        r"\1\2",
        texto
    )

    # Correções diretas vistas nos EPUBs
    correcoes = {
        "deTODOS.Cada": "de TODOS. Cada",
        "deTODOS": "de TODOS",
        "TODOS.Cada": "TODOS. Cada",
        "processo.A": "processo. A",
        "trama.Bruxas": "trama. Bruxas",
        "Sériemas": "Série, mas",
        "sérieSons": "série Sons",
        "passaapós": "passa após",
        "Weaknesseantes": "Weakness e antes",
        "4Playda": "4Play da",
        "completaPara": "completa. Para",
        "umaexperiência": "uma experiência",
        "cinematográficacompleta": "cinematográfica completa",
        "completaincluindo": "completa incluindo",
        "sitewww": "site www",
        "meu sitewww": "meu site www",
        "paraaNola": "para a Nola",
        "paraaNo": "para a No",
        "paraaNa": "para a Na",
        "tambémpara": "também para",
        "relacionamentoscruciais": "relacionamentos cruciais",
        "daA Saga": "da Saga",
        "emesse quarto": "nesse quarto",
        "emesse qu": "nesse qu",
        "quememória": "que memória",
        "quememoria": "que memória",
        "caralhoquememória": "caralho, que memória",
        "caralhoquememoria": "caralho, que memória",
        "bemEspero": "bem? Espero",
        "bem?Espero": "bem? Espero",
        "físicaSem": "física. Sem",
        "fisicaSem": "física. Sem",
        "físicasem": "física. Sem",
        "fisicasem": "física. Sem",
        "semviolência": "sem violência",
        "semviolencia": "sem violência",
        "seunúmero": "seu número",
        "seunumero": "seu número",
        "minhatristeza": "minha tristeza",
        "ignorá-lasMAS": "ignorá-las. MAS",
        "ignora-lasMAS": "ignorá-las. MAS",
        "eununcadeixarei": "eu nunca deixarei",
        "lágri mas": "lágrimas",
        "lá gri mas": "lágrimas",
        "lágr i mas": "lágrimas",
        "gr ito": "grito",
        "TO grito": "O grito",
        "TO gr ito": "O grito",
        "T O grito": "O grito",
        "memó ria": "memória",
        "fí sica": "física",
        "rá pido": "rápido",
        "cére bro": "cérebro",
        "conse guir": "conseguir",
        "sozin has": "sozinhas",
        "h is tória": "história",
        "his tória": "história",
    }

    for errado, certo in correcoes.items():
        texto = texto.replace(errado, certo)

    # Letras sobrando no começo de frase/trecho por causa de dropcap/OCR:
    # TO grito -> O grito | T A voz -> A voz
    texto = re.sub(r"(^|[.!?]\s+)T\s*O\s+([a-záàâãéêíóôõúç])", r"\1O \2", texto)
    texto = re.sub(r"(^|[.!?]\s+)T\s*A\s+([a-záàâãéêíóôõúç])", r"\1A \2", texto)
    texto = re.sub(r"(^|[.!?]\s+)T\s+(O|A|Os|As|Eu|Ele|Ela|Meu|Minha)\b", r"\1\2", texto)

    # Junta pedaços quebrados frequentes
    texto = re.sub(r"\blá\s*gri\s*mas\b", "lágrimas", texto, flags=re.I)
    texto = re.sub(r"\bgr\s*ito\b", "grito", texto, flags=re.I)
    texto = re.sub(r"\bmemó\s*ria\b", "memória", texto, flags=re.I)
    texto = re.sub(r"\bfí\s*sica\b", "física", texto, flags=re.I)
    texto = re.sub(r"\brá\s*pido\b", "rápido", texto, flags=re.I)
    texto = re.sub(r"\bcére\s*bro\b", "cérebro", texto, flags=re.I)
    texto = re.sub(r"\bconse\s*guir\b", "conseguir", texto, flags=re.I)
    texto = re.sub(r"\bsozin\s*has\b", "sozinhas", texto, flags=re.I)
    texto = re.sub(r"\bprotagonis\s*ta\b", "protagonista", texto, flags=re.I)
    texto = re.sub(r"\bhis\s*tória\b", "história", texto, flags=re.I)

    # Separa palavras grudadas com maiúscula: físicaSem, passaEm
    texto = re.sub(
        r"([a-záàâãéêíóôõúç])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,})",
        r"\1 \2",
        texto
    )

    # Corrige pontuação grudada
    texto = re.sub(r"([.!?;:])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇA-Za-zÀ-ÿ])", r"\1 \2", texto)

    # Separações específicas seguras
    texto = re.sub(
        r"\b([a-záàâãéêíóôõúç]{3,})(incluindo|experiência|história|memória|violência|física)\b",
        r"\1 \2",
        texto,
        flags=re.I
    )

    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"\s{2,}", " ", texto)

    # URLs
    texto = texto.replace("sitewww.", "site www.")
    texto = texto.replace("www. ", "www.")
    texto = texto.replace(". com", ".com")

    return texto.strip()


def texto_suspeito_para_gemini(texto):
    if not texto:
        return False

    t = str(texto).strip()

    if len(t) < 8:
        return False

    # Esses casos precisam de IA porque podem envolver contexto/tradução.
    padroes = [
        r"[a-záàâãéêíóôõúç]{3,}[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}",
        r"\b(lá\s*gri\s*mas|gr\s*ito|memó\s*ria|fí\s*sica|rá\s*pido|cére\s*bro|protagonis\s*ta|conse\s*guir)\b",
        r"\bTO\s+[a-záàâãéêíóôõúç]",
        r"\b(The|Sons of the Elite|Man's Weakness|Series|Play)\b",
    ]

    for p in padroes:
        if re.search(p, t, flags=re.I):
            return True

    suspeitas = [
        "deTODOS", "TODOS.Cada", "completaincluindo",
        "umaexperiência", "paraaNo", "passaEm",
        "deda", "dea", "doa", "quememória",
        "caralhoquememória", "bemEspero", "físicaSem",
        "semviolência", "lágri mas", "gr ito", "TO grito",
        "passaapós", "sérieSons", "4Playda",
    ]

    for item in suspeitas:
        if item.lower() in t.lower():
            return True

    return False




def nivel_gemini_atual(user_id=None):
    if user_id is None:
        return "leve"
    return usuarios.get(user_id, {}).get("nivel_gemini", "leve")


def texto_suspeito_para_gemini_nivel(texto, nivel="leve"):
    if not texto:
        return False

    t = str(texto).strip()

    if len(t) < 8:
        return False

    # Leve: foco real em palavras quebradas, sem mandar frases normais inteiras para o Gemini.
    padroes_leve = [
        r"\b[A-Za-zÀ-ÿ]{2,}\s*-\s+[a-záàâãéêíóôõúç]{2,}\b",
        r"\b(lá\s*gri\s*mas|gr\s*ito|memó\s*ria|fí\s*sica|rá\s*pido|cére\s*bro|protagonis\s*ta|conse\s*guir|li\s*berdades|algu\s*mas|análi\s*se|lib\s*erdades|histó\s*ria|polí\s*tico)\b",
        r"\bTO\s+[a-záàâãéêíóôõúç]",
    ]

    for p in padroes_leve:
        if re.search(p, t, flags=re.I):
            return True

    if nivel in ["media", "pesada"]:
        padroes_media = [
            r"[a-záàâãéêíóôõúç]{3,}[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}",
            r"([.!?;:])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇA-Za-zÀ-ÿ])",
            r"\s{2,}",
        ]

        for p in padroes_media:
            if re.search(p, t):
                return True

    if nivel == "pesada":
        # Pesada também revisa trechos um pouco estranhos, mas ainda sem reescrever história.
        if len(t) >= 50:
            return True

    return False


def prompt_gemini_por_nivel(texto, nivel="leve"):
    if nivel == "pesada":
        instrucoes = """
Você é revisor de EPUB em português brasileiro.

Revise o trecho com cuidado, mas SEM reescrever a história.

Pode corrigir:
- palavras separadas indevidamente;
- palavras quebradas por hífen/espaço;
- pontuação;
- espaçamento;
- pequenos erros visuais;
- fluidez leve quando a frase estiver estranha.

Não pode:
- mudar nomes próprios;
- traduzir nomes de personagens, cidades, países ou marcas;
- resumir;
- adicionar conteúdo;
- remover conteúdo;
- mudar o sentido;
- trocar palavrões por palavras suaves.
""".strip()
    elif nivel == "media":
        instrucoes = """
Você é revisor de EPUB em português brasileiro.

Corrija somente:
- palavras separadas indevidamente;
- palavras quebradas por hífen/espaço;
- pontuação grudada;
- espaços errados;
- pequenos erros visuais.

Não reescreva a história.
Não mude nomes próprios.
Não traduza nomes de personagens, cidades, países ou marcas.
Não adicione nem remova conteúdo.
""".strip()
    else:
        instrucoes = """
Você é revisor técnico de EPUB em português brasileiro.

Corrija APENAS:
- palavras separadas indevidamente;
- palavras quebradas por hífen ou espaço;
- letras soltas no começo quando for erro visual.

Não corrija estilo.
Não reescreva frases.
Não mude nomes próprios.
Não traduza nomes de personagens, cidades, países ou marcas.
Não adicione nem remova conteúdo.
""".strip()

    return f"""
{instrucoes}

Retorne SOMENTE o trecho corrigido, sem explicação.

Trecho:
{texto}
""".strip()


def gemini_revisar_trecho(texto, nivel="leve"):
    if not GEMINI_API_KEY or "COLE_SUA_CHAVE" in GEMINI_API_KEY:
        return remover_sujeiras_texto(texto)

    prompt = prompt_gemini_por_nivel(texto, nivel)

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
            "temperature": 0.05 if nivel == "leve" else 0.1,
            "topP": 0.8,
            "maxOutputTokens": 1400,
        }
    }

    try:
        resposta = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)

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
    return limpar_texto_inteligente(texto)

def revisar_html_simples(html):
    soup = BeautifulSoup(html, "html.parser")

    # Primeiro tenta corrigir por blocos de texto.
    # Isso pega erros espalhados entre spans, como "protagonis- ta".
    blocos = soup.find_all(["p", "div", "span", "li", "blockquote"])

    for tag in blocos:
        if tag.name in ["script", "style", "code", "pre", "head", "title"]:
            continue

        if tag.find(["p", "div", "li", "blockquote"]):
            continue

        if tag.find("img"):
            continue

        original = tag.get_text(" ", strip=True)

        if not original:
            continue

        if len(original) > MAX_CARACTERES:
            continue

        novo = remover_sujeiras_texto(original)
        novo = corrigir_palavras_grudadas(novo)
        novo = limpar_texto_inteligente(novo)

        if novo and novo != original:
            tag.clear()
            tag.append(NavigableString(novo))

    return str(soup)

def revisar_html_gemini(html, nivel='leve'):
    soup = BeautifulSoup(html, "html.parser")
    corrigidos = 0
    chamadas = 0

    MAX_CARACTERES = 450

    blocos = soup.find_all(["p", "div", "span", "li", "blockquote"])

    for tag in blocos:
        if tag.name in ["script", "style", "code", "pre", "head", "title"]:
            continue

        if tag.find(["p", "div", "li", "blockquote"]):
            continue

        if tag.find("img"):
            continue

        original = tag.get_text(" ", strip=True)

        if not original:
            continue

        if len(original) > MAX_CARACTERES:
            continue

        novo = remover_sujeiras_texto(original)
        novo = corrigir_palavras_grudadas(novo)
        novo = limpar_texto_inteligente(novo)

        usar_gemini = texto_suspeito_para_gemini_nivel(novo, nivel) and chamadas < MAX_GEMINI_TRECHOS

        if usar_gemini:
            try:
                revisado = gemini_revisar_trecho(novo, nivel)
                chamadas += 1

                if (
                    revisado
                    and len(revisado) >= max(3, int(len(novo) * 0.55))
                    and len(revisado) <= max(120, int(len(novo) * 1.9))
                ):
                    novo = limpar_texto_inteligente(revisado)
            except Exception:
                pass

        if novo and novo != original:
            tag.clear()
            tag.append(NavigableString(novo))
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


def revisar_epub_com_gemini(entrada, saida, progresso_callback=None, nivel='leve', user_id=None):
    book = epub.read_epub(str(entrada))
    docs = list(book.get_items_of_type(ITEM_DOCUMENT))
    total = len(docs) or 1
    total_corrigidos = 0

    for i, item in enumerate(docs, start=1):
        if user_id is not None and user_id in cancelamentos:
            raise Exception("Revisão cancelada.")

        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            html, corrigidos = revisar_html_gemini(html, nivel=nivel)
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


def pegar_todas_imagens_epub(caminho_epub, limite=30):
    book = epub.read_epub(str(caminho_epub))
    imagens = list(book.get_items_of_type(ITEM_IMAGE))

    # Capa primeiro, depois demais.
    imagens_ordenadas = []
    for img in imagens:
        nome = (img.file_name or "").lower()
        if "cover" in nome or "capa" in nome:
            imagens_ordenadas.append(img)

    for img in imagens:
        if img not in imagens_ordenadas:
            imagens_ordenadas.append(img)

    return imagens_ordenadas[:limite]


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



def limpar_epub_para_calibre(caminho_epub):
    """
    Remove META-INF/encryption.xml antes do Calibre.
    Isso corrige EPUB -> PDF em livros com fonte ofuscada/proteção leve.
    """
    caminho_epub = Path(caminho_epub)

    if caminho_epub.suffix.lower() != ".epub":
        return caminho_epub

    saida = TEMP_DIR / f"calibre_limpo_{uuid.uuid4().hex}.epub"

    try:
        with zipfile.ZipFile(caminho_epub, "r") as zin:
            with zipfile.ZipFile(saida, "w") as zout:
                for item in zin.infolist():
                    nome = item.filename.replace("\\", "/").lower()

                    if nome == "meta-inf/encryption.xml":
                        continue

                    dados = zin.read(item.filename)
                    zout.writestr(item, dados)

        return saida

    except Exception:
        return caminho_epub


def ebook_convert_disponivel():
    return shutil.which("ebook-convert") is not None
    

def converter_com_calibre(entrada, saida):
    if not ebook_convert_disponivel():
        raise Exception(
            "O conversor do Calibre não foi encontrado. "
            "Instale o Calibre ou deixe o comando ebook-convert disponível."
        )

    entrada = Path(entrada)
    saida = Path(saida)

    entrada_convertida = entrada

    if entrada.suffix.lower() == ".epub":
        saida = saida.with_suffix(".pdf")
        entrada_convertida = limpar_epub_para_calibre(entrada)

    elif entrada.suffix.lower() == ".pdf":
        saida = saida.with_suffix(".epub")

    else:
        raise Exception("Formato não suportado. Use apenas EPUB ou PDF.")

    env = os.environ.copy()

    # Correções para Railway/servidor sem tela/GPU.
    env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--no-sandbox --disable-gpu --disable-software-rasterizer"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_BACKEND"] = "software"
    env["QT_OPENGL"] = "software"
    env["QT_XCB_GL_INTEGRATION"] = "none"
    env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    env["MESA_LOADER_DRIVER_OVERRIDE"] = "llvmpipe"
    env["XDG_RUNTIME_DIR"] = str(TEMP_DIR)

    base_cmd = [
        "xvfb-run",
        "-a",
        "ebook-convert",
        str(entrada_convertida),
        str(saida),
        "--pdf-page-numbers",
        "--disable-font-rescaling",
    ]

    # Em servidor sem display, o Calibre às vezes só funciona com xvfb-run.
    xvfb = shutil.which("xvfb-run")
    if xvfb:
        comando = [xvfb, "-a", "--server-args=-screen 0 1024x768x24"] + base_cmd
    else:
        comando = base_cmd

    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=1200,
        env=env,
    )

    try:
        if entrada_convertida != entrada and Path(entrada_convertida).exists():
            Path(entrada_convertida).unlink(missing_ok=True)
    except Exception:
        pass

    if resultado.returncode != 0:
        erro = resultado.stderr[-1800:] or resultado.stdout[-1800:] or "Falha na conversão."

        if "QVulkanInstance" in erro or "Vulkan" in erro or "GPU vendor" in erro:
            erro = (
                "Erro do Calibre no servidor sem GPU/tela. "
                "Precisa instalar dependências gráficas no Dockerfile: xvfb, libegl1, libgl1, libopengl0, libxcb-cursor0.\\n\\n"
                + erro
            )

        raise Exception(erro)


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


def traduzir_texto_google_simples(texto):
    """
    Tradução por GoogleTranslator, sem Gemini.
    Mantém nomes próprios naturalmente quando o tradutor não altera.
    """
    texto = (texto or "").strip()
    if not texto:
        return ""

    if GoogleTranslator is None:
        return texto

    try:
        return GoogleTranslator(source="auto", target="pt").translate(texto).strip()
    except Exception:
        return texto


def carregar_fonte_ajustada(tamanho):
    if ImageFont is None:
        return None

    fontes = [
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
        "Arial.ttf",
        "arial.ttf",
    ]

    for nome in fontes:
        try:
            return ImageFont.truetype(nome, tamanho)
        except Exception:
            pass

    return ImageFont.load_default()


def cor_media_area(img, x, y, w, h):
    try:
        crop = img.crop((x, y, x + w, y + h)).resize((1, 1))
        return crop.getpixel((0, 0))
    except Exception:
        return (255, 255, 255)


def brilho(cor):
    try:
        r, g, b = cor[:3]
        return (r * 299 + g * 587 + b * 114) / 1000
    except Exception:
        return 255


def quebrar_texto_largura(draw, texto, fonte, largura_max):
    linhas = []
    for bloco in str(texto).splitlines():
        bloco = bloco.strip()
        if not bloco:
            continue

        palavras = bloco.split()
        atual = ""

        for palavra in palavras:
            teste = palavra if not atual else atual + " " + palavra
            try:
                box = draw.textbbox((0, 0), teste, font=fonte)
                tw = box[2] - box[0]
            except Exception:
                tw = len(teste) * 10

            if tw <= largura_max:
                atual = teste
            else:
                if atual:
                    linhas.append(atual)
                atual = palavra

        if atual:
            linhas.append(atual)

    return linhas or [texto]


def ocr_linhas_imagem(imagem):
    """
    OCR local estilo Google Tradutor.
    Precisa de pytesseract + tesseract instalado no ambiente.
    Retorna linhas com texto e caixa aproximada.
    """
    if pytesseract is None:
        raise Exception(
            "OCR não instalado. Adicione pytesseract no requirements.txt e Tesseract no Dockerfile."
        )

    img = imagem.convert("RGB")

    tentativas = [
        {"escala": 3, "psm": 6, "conf": 12},
        {"escala": 4, "psm": 6, "conf": 8},
        {"escala": 3, "psm": 11, "conf": 8},
        {"escala": 4, "psm": 11, "conf": 5},
    ]

    melhor = []

    for tentativa in tentativas:
        escala = tentativa["escala"]
        psm = tentativa["psm"]
        conf_min = tentativa["conf"]

        grande = img.resize((img.width * escala, img.height * escala))
        cinza = ImageOps.grayscale(grande)
        cinza = ImageOps.autocontrast(cinza)

        # Aumenta contraste para textos pequenos/escuros.
        try:
            cinza = cinza.point(lambda p: 255 if p > 145 else 0)
        except Exception:
            pass

        config = f"--psm {psm}"

        try:
            dados = pytesseract.image_to_data(
                cinza,
                lang="eng",
                config=config,
                output_type=pytesseract.Output.DICT,
            )
        except Exception as erro:
            raise Exception(f"OCR falhou. Verifique se o Tesseract está instalado no Dockerfile. Detalhe: {erro}")

        grupos = {}
        n = len(dados.get("text", []))

        for i in range(n):
            txt = (dados["text"][i] or "").strip()
            conf = dados.get("conf", ["0"])[i]

            try:
                conf_val = float(conf)
            except Exception:
                conf_val = 0

            if not txt or conf_val < conf_min:
                continue

            # filtra ruído puro
            if not re.search(r"[A-Za-z]", txt):
                continue

            key = (
                dados.get("block_num", [0])[i],
                dados.get("par_num", [0])[i],
                dados.get("line_num", [0])[i],
            )

            x = int(dados["left"][i] / escala)
            y = int(dados["top"][i] / escala)
            w = max(1, int(dados["width"][i] / escala))
            h = max(1, int(dados["height"][i] / escala))

            grupos.setdefault(key, []).append((txt, x, y, w, h))

        linhas = []

        for itens in grupos.values():
            texto = " ".join(t[0] for t in itens).strip()
            texto = re.sub(r"\s+", " ", texto)

            if not texto or len(texto) < 2:
                continue

            xs = [t[1] for t in itens]
            ys = [t[2] for t in itens]
            x2s = [t[1] + t[3] for t in itens]
            y2s = [t[2] + t[4] for t in itens]

            x1 = max(0, min(xs) - 6)
            y1 = max(0, min(ys) - 6)
            x2 = min(img.width, max(x2s) + 6)
            y2 = min(img.height, max(y2s) + 6)

            if (x2 - x1) < 8 or (y2 - y1) < 8:
                continue

            linhas.append({
                "texto": texto,
                "x": x1,
                "y": y1,
                "w": x2 - x1,
                "h": y2 - y1,
            })

        if len(linhas) > len(melhor):
            melhor = linhas

        if len(melhor) >= 2:
            break

    return melhor


def criar_imagem_estilo_google_tradutor(imagem_bytes):
    """
    Traduz imagem sem Gemini:
    - OCR com pytesseract;
    - tradução com GoogleTranslator;
    - cobre o texto original com cor média local;
    - escreve o texto traduzido no mesmo lugar.
    Fica parecido com Google Tradutor, dentro do possível sem o serviço oficial.
    """
    if Image is None:
        raise Exception("Pillow não instalado. Adicione Pillow no requirements.txt")

    imagem = Image.open(BytesIO(imagem_bytes)).convert("RGB")
    nova = imagem.copy()
    draw = ImageDraw.Draw(nova, "RGBA")

    linhas = ocr_linhas_imagem(imagem)

    if not linhas:
        raise Exception("Não encontrei texto legível nessa imagem pelo OCR.")

    traduzidas = []

    for item in linhas:
        original = item["texto"]
        traduzido = traduzir_texto_google_simples(original)

        if not traduzido:
            traduzido = original

        x, y, w, h = item["x"], item["y"], item["w"], item["h"]

        # Área um pouco maior para cobrir texto original.
        pad = max(4, int(h * 0.35))
        rx = max(0, x - pad)
        ry = max(0, y - pad)
        rw = min(nova.width - rx, w + pad * 2)
        rh = min(nova.height - ry, h + pad * 2)

        fundo = cor_media_area(imagem, rx, ry, rw, rh)
        texto_cor = (20, 20, 20) if brilho(fundo) > 145 else (235, 235, 235)

        # Cobre o texto original com retângulo suave/translúcido parecido com o fundo.
        draw.rounded_rectangle(
            (rx, ry, rx + rw, ry + rh),
            radius=max(2, int(h * 0.25)),
            fill=(fundo[0], fundo[1], fundo[2], 230),
        )

        # Tamanho da fonte baseado na altura original.
        fonte_tam = max(10, min(60, int(h * 0.95)))
        fonte = carregar_fonte_ajustada(fonte_tam)

        # Reduz fonte se a tradução ficar grande.
        for tam in range(fonte_tam, 8, -1):
            fonte = carregar_fonte_ajustada(tam)
            linhas_texto = quebrar_texto_largura(draw, traduzido, fonte, rw)
            try:
                box = draw.textbbox((0, 0), "Ag", font=fonte)
                lh = max(10, box[3] - box[1] + max(2, tam // 4))
            except Exception:
                lh = tam + 4

            if len(linhas_texto) * lh <= rh + int(h * 0.8):
                break

        linhas_texto = quebrar_texto_largura(draw, traduzido, fonte, rw)

        try:
            box = draw.textbbox((0, 0), "Ag", font=fonte)
            lh = max(10, box[3] - box[1] + max(2, fonte_tam // 5))
        except Exception:
            lh = fonte_tam + 4

        total_h = len(linhas_texto) * lh
        ty = ry + max(0, (rh - total_h) // 2)

        for linha in linhas_texto:
            try:
                tb = draw.textbbox((0, 0), linha, font=fonte)
                tw = tb[2] - tb[0]
            except Exception:
                tw = len(linha) * fonte_tam // 2

            tx = rx + max(0, (rw - tw) // 2)

            # sombra leve
            sombra = (0, 0, 0, 80) if brilho(texto_cor) > 145 else (255, 255, 255, 80)
            draw.text((tx + 1, ty + 1), linha, fill=sombra, font=fonte)
            draw.text((tx, ty), linha, fill=texto_cor + (255,), font=fonte)
            ty += lh

        traduzidas.append(f"{original} → {traduzido}")

    buffer = BytesIO()
    nova.save(buffer, format="JPEG", quality=94)
    return buffer.getvalue(), traduzidas


def buscar_bytes_imagem_epub(entrada, nome_imagem):
    book = epub.read_epub(str(entrada))
    for item in book.get_items_of_type(ITEM_IMAGE):
        if item.file_name == nome_imagem:
            media_type = getattr(item, "media_type", "") or "image/jpeg"
            return item.get_content(), media_type
    return None, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not autorizado(user_id):
        await update.message.reply_text("⛔ Você não tem acesso ao Alma Scriptum Studio.")
        return

    cancelamentos.add(user_id)
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
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "revisar"
        await query.message.reply_text(
            "🛠 Modo Revisar / Limpar EPUB\n\n"
            "Envie o EPUB traduzido para eu limpar sujeiras de site e organizar o texto."
        )

    elif data == "modo_gemini_menu":
        await query.message.reply_text(
            "🤖 Revisar com Gemini\n\n"
            "Escolha o nível da revisão:",
            reply_markup=painel_gemini(),
        )

    elif data == "gemini_leve":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "gemini"
        usuarios[user_id]["nivel_gemini"] = "leve"
        await query.message.reply_text(
            "🟢 Revisão leve ativada.\n\n"
            "Foco:\n"
            "• palavras separadas\n"
            "• palavras quebradas\n"
            "• erros leves de espaçamento\n\n"
            "Envie o EPUB já traduzido."
        )

    elif data == "gemini_media":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "gemini"
        usuarios[user_id]["nivel_gemini"] = "media"
        await query.message.reply_text(
            "🟡 Revisão média ativada.\n\n"
            "Foco:\n"
            "• palavras separadas\n"
            "• pontuação grudada\n"
            "• pequenos erros visuais\n\n"
            "Envie o EPUB já traduzido."
        )

    elif data == "gemini_pesada":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "gemini"
        usuarios[user_id]["nivel_gemini"] = "pesada"
        await query.message.reply_text(
            "🔴 Revisão pesada ativada.\n\n"
            "Foco:\n"
            "• revisão mais forte\n"
            "• fluidez leve\n"
            "• erros difíceis\n\n"
            "Sem mudar nomes próprios nem a história.\n\n"
            "Envie o EPUB já traduzido."
        )

    elif data == "modo_imagens":
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "imagens"
        await query.message.reply_text(
            "🖼 Traduzir / trocar imagens\n\n"
            "Envie o EPUB.\n"
            "Vou mostrar as imagens encontradas para você escolher qual deseja trocar/traduzir."
        )

    elif data == "modo_capa":
        cancelamentos.discard(user_id)
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
        cancelamentos.discard(user_id)
        usuarios[user_id]["modo"] = "epub_pdf"
        await query.message.reply_text("📘 Envie o EPUB que deseja converter para PDF.")

    elif data == "conv_pdf_epub":
        cancelamentos.discard(user_id)
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

    elif data.startswith("traduzir_img_"):
        indice = int(data.replace("traduzir_img_", "")) - 1
        dados = usuarios.get(user_id, {})
        entrada = dados.get("capa_entrada")
        imagens = dados.get("capa_imagens", [])

        if not entrada or indice < 0 or indice >= len(imagens):
            await query.message.reply_text("⚠️ Não encontrei essa imagem. Envie o EPUB novamente.")
            return

        nome_imagem = imagens[indice]
        msg = await query.message.reply_text("🌐 Traduzindo imagem no estilo Google Tradutor...")

        try:
            imagem_bytes, media_type = buscar_bytes_imagem_epub(entrada, nome_imagem)

            if not imagem_bytes:
                await msg.edit_text("⚠️ Não consegui localizar a imagem dentro do EPUB.")
                return

            loop = asyncio.get_running_loop()
            nova_bytes, traducoes = await loop.run_in_executor(
                None,
                lambda: criar_imagem_estilo_google_tradutor(imagem_bytes)
            )

            preview = TEMP_DIR / f"imagem_traduzida_{uuid.uuid4().hex}.jpg"
            preview.write_bytes(nova_bytes)

            resumo = "\n".join(traducoes[:8])
            if len(resumo) > 900:
                resumo = resumo[:900] + "..."

            with open(preview, "rb") as img_file:
                await query.message.reply_photo(
                    photo=img_file,
                    caption=(
                        f"✅ Imagem {indice + 1} traduzida no estilo Google Tradutor.\n\n"
                        "O EPUB ainda NÃO foi alterado.\n"
                        "Se gostar, toque em 🔁 Trocar imagem e envie esta imagem traduzida.\n\n"
                        f"Trechos:\n{resumo}"
                    )
                )

            await msg.edit_text("✅ Tradução da imagem concluída. O EPUB ainda não foi alterado.")
            preview.unlink(missing_ok=True)

        except Exception as erro:
            await query.message.reply_text(
                "❌ Não consegui traduzir essa imagem automaticamente.\n\n"
                f"Motivo: {erro}\n\n"
                "Dica: imagens com texto muito pequeno/escuro podem precisar de edição manual ou OCR melhor."
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
            "🔁 Envie agora a nova imagem traduzida.\n\n"
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
            await query.message.reply_text("✅ Nenhuma imagem foi marcada para remover.\n\nSe você já trocou uma imagem, o EPUB atualizado já foi enviado na troca.")
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
        cancelamentos.add(user_id)
        limpar_sessao_capa(user_id)
        usuarios[user_id] = {"modo": None}
        await query.message.reply_text("❌ Cancelamento solicitado. Se houver revisão rodando, ela vai parar no próximo arquivo interno.")


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not autorizado(user_id):
        return

    cancelamentos.add(user_id)
    limpar_sessao_capa(user_id)
    usuarios[user_id] = {"modo": None}

    await update.message.reply_text(
        "❌ Cancelamento solicitado.\n"
        "Use /start para abrir o painel novamente."
    )


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

            nivel = usuarios.get(user_id, {}).get("nivel_gemini", "leve")
            cancelamentos.discard(user_id)

            loop = asyncio.get_running_loop()
            corrigidos = await loop.run_in_executor(
                None,
                lambda: revisar_epub_com_gemini(entrada, saida, nivel=nivel, user_id=user_id)
            )

            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 85, "📦 Preparando EPUB revisado...")

            with open(saida, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=nome_epub(nome_original)),
                    caption=f"✅ Revisão com Gemini concluída.\n🧠 Nível: {usuarios.get(user_id, {}).get('nivel_gemini', 'leve')}\n🧩 Trechos ajustados: {corrigidos}",
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=90,
                    pool_timeout=90,
                )

            await atualizar_carregamento(msg, "🤖 Revisão com Gemini", 100, "✅ EPUB revisado e enviado.")

        elif modo == "imagens":
            if not nome_original.lower().endswith(".epub"):
                await update.message.reply_text("⚠️ Envie apenas EPUB para traduzir/trocar imagens.")
                return

            msg = await update.message.reply_text("🖼 Preparando imagens do EPUB...")
            await atualizar_carregamento(msg, "🖼 Traduzir / trocar imagens", 20, "📥 EPUB recebido. Procurando imagens...")

            imagens = pegar_todas_imagens_epub(entrada, limite=30)

            usuarios[user_id]["capa_entrada"] = str(entrada)
            usuarios[user_id]["capa_nome_original"] = nome_original
            usuarios[user_id]["capa_imagens"] = [img.file_name for img in imagens]
            usuarios[user_id]["remover_imagens"] = []

            await atualizar_carregamento(msg, "🖼 Traduzir / trocar imagens", 60, f"🖼 Encontrei {len(imagens)} imagem(ns). Enviando prévias...")

            if not imagens:
                await atualizar_carregamento(msg, "🖼 Traduzir / trocar imagens", 100, "⚠️ Não encontrei imagens no EPUB.")
                return

            for i, img in enumerate(imagens, start=1):
                img_path = salvar_imagem_temp(img)

                try:
                    with open(img_path, "rb") as img_file:
                        await update.message.reply_photo(
                            photo=img_file,
                            caption=f"🖼 Imagem {i}\nArquivo interno: {img.file_name}\n\nPara trocar/traduzir, toque em 🔁 Trocar imagem {i}.",
                            reply_markup=InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton(f"🌐 Traduzir imagem {i}", callback_data=f"traduzir_img_{i}"),
                                ],
                                [
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
                "🖼 Traduzir / trocar imagens",
                100,
                "✅ Imagens enviadas.\n\nEscolha 🔁 Trocar na imagem desejada, envie a imagem traduzida, e depois finalize.",
            )

            return

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
            if modo not in ["capa", "imagens"]:
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

    msg = await update.message.reply_text("🔁 Preparando troca de capa...")

    try:
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 40, "📥 Nova imagem recebida...")

        with open(nova_capa, "rb") as f:
            nova_bytes = f.read()

        await atualizar_carregamento(msg, "🔁 Trocando imagem", 70, "🖼 Substituindo imagem escolhida...")

        trocar_imagem_epub(entrada, saida, nome_imagem, nova_bytes)

        await atualizar_carregamento(msg, "🔁 Trocando imagem", 90, "📦 Preparando EPUB atualizado...")

        with open(saida, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=nome_epub(nome_original)),
                caption="✅ Imagem trocada e EPUB atualizado.",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=90,
                pool_timeout=90,
            )

        await atualizar_carregamento(msg, "🔁 Trocando imagem", 100, "✅ Imagem trocada e enviada.")

    except Exception as erro:
        await update.message.reply_text(f"❌ Erro ao trocar capa:\n{erro}")

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

    nome_imagem = imagens[indice]

    arquivo = await documento.get_file()
    nova_capa = TEMP_DIR / f"nova_imagem_{uuid.uuid4().hex}_{nome}"
    await arquivo.download_to_drive(str(nova_capa))

    saida = TEMP_DIR / nome_epub(nome_original)
    msg = await update.message.reply_text("🔁 Preparando troca de imagem...")

    try:
        await atualizar_carregamento(msg, "🔁 Trocando imagem", 40, "📥 Nova imagem recebida...")

        with open(nova_capa, "rb") as f:
            nova_bytes = f.read()

        await atualizar_carregamento(msg, "🔁 Trocando imagem", 70, "🖼 Substituindo imagem escolhida...")

        trocar_imagem_epub(entrada, saida, nome_imagem, nova_bytes)

        await atualizar_carregamento(msg, "🔁 Trocando imagem", 90, "📦 Preparando EPUB atualizado...")

        with open(saida, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=nome_epub(nome_original)),
                caption="✅ Imagem trocada e EPUB atualizado.",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=90,
                pool_timeout=90,
            )

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
        .read_timeout(180)
        .write_timeout(180)
        .connect_timeout(90)
        .pool_timeout(90)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    app.add_handler(MessageHandler(filters.Document.ALL, receber_documento_imagem))

    print("✅ Alma Scriptum Studio ONLINE")
    app.run_polling()


if __name__ == "__main__":
    main()
