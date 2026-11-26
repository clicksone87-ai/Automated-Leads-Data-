"""Fixed email sentences per language. A language is usable only when it is complete here."""

from __future__ import annotations

import re
from dataclasses import dataclass

from leads.models import WebsiteStatus
from leads.outreach.templates import language_for_country


@dataclass(frozen=True, slots=True)
class Phrases:
    name: str  # for the AI instructions and the dashboard
    greeting: str
    regards: str
    team: str
    opt_out: str
    openings: dict[str, tuple[str, ...]]
    words: tuple[int, int]  # accepted length of the AI text
    # Regex of claims the AI must not make in this language: about the business's present
    # website, reviews, awards, guarantees, percentages.
    judges: str = ""
    script: str = "latin"  # letters the text is written in: latin, arabic or cyrillic


EN = Phrases(
    name="British English",
    greeting="Hi {name} team,",
    regards="Best regards,",
    team="The {company} team",
    opt_out="If you'd rather not hear from us, reply \"no thanks\" and we won't contact you again.",
    # "None" is absence of evidence and is worded as such.
    openings={
        WebsiteStatus.NONE: (
            "We came across {name} and couldn't find a website for the business.",
            "We looked for a website for {name} and couldn't find one.",
            "We came across {name}, but not a website for it.",
        ),
        WebsiteStatus.BROKEN: (
            "We tried to visit {domain} and the site didn't load properly for us.",
            "We came across {name}, but {domain} didn't open properly when we tried it.",
        ),
        WebsiteStatus.PARKED: (
            "We came across {name}, but {domain} showed only a placeholder page when we opened it.",
        ),
        WebsiteStatus.REDIRECT_SOCIAL: (
            "We came across {name} and found a social media page, but no separate website.",
        ),
        WebsiteStatus.WORKING: ("We came across {name} and your website at {domain}.",),
    },
    words=(30, 140),
)

AR = Phrases(
    name="Modern Standard Arabic",
    greeting="فريق {name} المحترم،\nالسلام عليكم ورحمة الله وبركاته،",
    regards="مع خالص التحية،",
    team="فريق {company}",
    opt_out=(
        "إذا كنتم لا ترغبون في تلقي رسائل منا، يكفي الرد بعبارة «لا شكرا» ولن نتواصل معكم مرة أخرى."
    ),
    openings={
        WebsiteStatus.NONE: (
            "اطّلعنا على نشاط {name} ولم نعثر على موقع إلكتروني له.",
            "بحثنا عن موقع إلكتروني لـ {name} ولم نعثر عليه.",
        ),
        WebsiteStatus.BROKEN: (
            "حاولنا زيارة {domain} ولكن الموقع لم يُفتح لدينا بشكل صحيح.",
            "اطّلعنا على نشاط {name}، ولكن الموقع {domain} لم يُفتح بشكل صحيح عندما حاولنا زيارته.",
        ),
        WebsiteStatus.PARKED: (
            "اطّلعنا على نشاط {name}، ولكن {domain} عرض لنا صفحة مؤقتة فقط عند فتحه.",
        ),
        WebsiteStatus.REDIRECT_SOCIAL: (
            "اطّلعنا على نشاط {name} ووجدنا صفحة على وسائل التواصل الاجتماعي، "
            "ولم نعثر على موقع إلكتروني مستقل.",
        ),
        WebsiteStatus.WORKING: ("اطّلعنا على نشاط {name} وعلى موقعكم الإلكتروني {domain}.",),
    },
    words=(25, 130),
    script="arabic",
)

_S = WebsiteStatus

FR = Phrases(
    name="French",
    greeting="Bonjour à toute l'équipe de {name},",
    regards="Bien cordialement,",
    team="L'équipe {company}",
    opt_out=(
        "Si vous ne souhaitez plus recevoir nos messages, répondez simplement « non merci » "
        "et nous ne vous contacterons plus."
    ),
    openings={
        _S.NONE: (
            "Nous avons découvert {name} et n'avons pas trouvé de site web pour l'entreprise.",
            "Nous avons cherché un site web pour {name}, sans en trouver.",
            "Nous avons découvert {name}, mais pas de site web.",
        ),
        _S.BROKEN: (
            "Nous avons essayé de consulter {domain}, mais le site ne s'est pas affiché "
            "correctement.",
            "Nous avons découvert {name}, mais {domain} ne s'est pas ouvert correctement "
            "lorsque nous avons essayé.",
        ),
        _S.PARKED: (
            "Nous avons découvert {name}, mais {domain} n'affichait qu'une page d'attente "
            "lorsque nous l'avons ouvert.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Nous avons découvert {name} et trouvé une page sur les réseaux sociaux, mais pas "
            "de site web distinct.",
        ),
        _S.WORKING: ("Nous avons découvert {name} ainsi que votre site web {domain}.",),
    },
    words=(30, 150),
    judges=(
        r"votre site|votre présence|votre page web|site actuel|site existant|obsolète|démodé|"
        r"vous manque|il vous manque|avis clients|récompense|garanti|pour cent"
    ),
)

DE = Phrases(
    name="German (formal Sie)",
    greeting="Sehr geehrtes Team von {name},",
    regards="Mit freundlichen Grüßen",
    team="Ihr {company}-Team",
    opt_out=(
        "Wenn Sie keine weiteren Nachrichten von uns wünschen, antworten Sie einfach mit "
        "„Nein danke“, und wir melden uns nicht mehr."
    ),
    openings={
        _S.NONE: (
            "Wir sind auf {name} aufmerksam geworden und konnten keine Website des "
            "Unternehmens finden.",
            "Wir haben nach einer Website von {name} gesucht und keine gefunden.",
            "Wir sind auf {name} gestoßen, aber nicht auf eine Website.",
        ),
        _S.BROKEN: (
            "Wir wollten {domain} aufrufen, doch die Seite wurde bei uns nicht richtig geladen.",
            "Wir sind auf {name} gestoßen, aber {domain} ließ sich bei unserem Versuch nicht "
            "richtig öffnen.",
        ),
        _S.PARKED: (
            "Wir sind auf {name} gestoßen, aber unter {domain} wurde uns nur eine "
            "Platzhalterseite angezeigt.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Wir sind auf {name} gestoßen und haben eine Seite in den sozialen Medien gefunden, "
            "aber keine eigene Website.",
        ),
        _S.WORKING: ("Wir sind auf {name} und Ihre Website {domain} gestoßen.",),
    },
    words=(25, 130),
    judges=(
        r"Ihre (?:Web)?seite|Ihre Website|Ihr(?:en)? Internetauftritt|Online-Präsenz|"
        r"aktuelle(?:n)? (?:Web)?s(?:ei)?te|bestehende(?:n)? (?:Web)?s(?:ei)?te|veraltet|"
        r"fehlt Ihnen|Bewertung|Auszeichnung|garantiert|Prozent"
    ),
)

ES = Phrases(
    name="Spanish (formal usted)",
    greeting="Estimado equipo de {name}:",
    regards="Saludos cordiales,",
    team="El equipo de {company}",
    opt_out=(
        "Si prefiere no recibir más mensajes nuestros, responda «no gracias» y no volveremos "
        "a contactarle."
    ),
    openings={
        _S.NONE: (
            "Encontramos {name} y no pudimos dar con un sitio web de la empresa.",
            "Buscamos un sitio web de {name} y no encontramos ninguno.",
            "Encontramos {name}, pero no un sitio web.",
        ),
        _S.BROKEN: (
            "Intentamos visitar {domain} y el sitio no se cargó correctamente.",
            "Encontramos {name}, pero {domain} no se abrió correctamente cuando lo intentamos.",
        ),
        _S.PARKED: (
            "Encontramos {name}, pero {domain} solo mostraba una página provisional al abrirlo.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Encontramos {name} y una página en redes sociales, pero no un sitio web propio.",
        ),
        _S.WORKING: ("Encontramos {name} y su sitio web {domain}.",),
    },
    words=(30, 150),
    judges=(
        r"su (?:sitio|página) web|su web\b|su presencia en (?:línea|internet)|sitio actual|"
        r"sitio existente|anticuad|desactualizad|le falta|carece|reseña|premio|garantiza|"
        r"por ciento"
    ),
)

IT = Phrases(
    name="Italian (formal)",
    greeting="Gentile team di {name},",
    regards="Cordiali saluti,",
    team="Il team di {company}",
    opt_out=(
        "Se preferisce non ricevere altri messaggi, risponda semplicemente «no grazie» e non "
        "la contatteremo più."
    ),
    openings={
        _S.NONE: (
            "Abbiamo conosciuto {name} e non abbiamo trovato un sito web dell'attività.",
            "Abbiamo cercato un sito web di {name} senza trovarne uno.",
            "Abbiamo conosciuto {name}, ma non un suo sito web.",
        ),
        _S.BROKEN: (
            "Abbiamo provato a visitare {domain}, ma il sito non si è caricato correttamente.",
            "Abbiamo conosciuto {name}, ma {domain} non si è aperto correttamente quando "
            "abbiamo provato.",
        ),
        _S.PARKED: (
            "Abbiamo conosciuto {name}, ma aprendo {domain} abbiamo visto solo una pagina "
            "provvisoria.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Abbiamo conosciuto {name} e trovato una pagina sui social, ma non un sito web "
            "dedicato.",
        ),
        _S.WORKING: ("Abbiamo conosciuto {name} e il vostro sito web {domain}.",),
    },
    words=(30, 150),
    judges=(
        r"vostro sito|suo sito|presenza online|sito attuale|sito esistente|obsolet|antiquat|"
        r"vi manca|le manca|recension|premiat|garanti|per cento"
    ),
)

PT = Phrases(
    name="Brazilian Portuguese",
    greeting="Olá, equipe {name},",
    regards="Atenciosamente,",
    team="Equipe {company}",
    opt_out=(
        "Se preferir não receber mais mensagens nossas, basta responder “não, obrigado” e não "
        "entraremos mais em contato."
    ),
    openings={
        _S.NONE: (
            "Encontramos {name} e não localizamos um site da empresa.",
            "Procuramos um site de {name} e não encontramos nenhum.",
            "Encontramos {name}, mas nenhum site.",
        ),
        _S.BROKEN: (
            "Tentamos acessar {domain}, mas o site não carregou corretamente.",
            "Encontramos {name}, mas {domain} não abriu corretamente quando tentamos.",
        ),
        _S.PARKED: (
            "Encontramos {name}, mas {domain} mostrava apenas uma página provisória quando o "
            "abrimos.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Encontramos {name} e uma página nas redes sociais, mas nenhum site próprio.",
        ),
        _S.WORKING: ("Encontramos {name} e o seu site {domain}.",),
    },
    words=(30, 150),
    judges=(
        r"seu site|sua presença (?:online|digital)|site atual|site existente|desatualizad|"
        r"ultrapassad|falta a vocês|avaliaç|prêmio|prémio|garanti|por cento"
    ),
)

NL = Phrases(
    name="Dutch (formal u)",
    greeting="Beste team van {name},",
    regards="Met vriendelijke groet,",
    team="Het team van {company}",
    opt_out=(
        "Wilt u liever geen berichten meer van ons ontvangen? Antwoord dan gewoon „nee "
        "bedankt” en we nemen geen contact meer met u op."
    ),
    openings={
        _S.NONE: (
            "We kwamen {name} tegen en konden geen website van het bedrijf vinden.",
            "We hebben gezocht naar een website van {name}, maar vonden er geen.",
            "We kwamen {name} tegen, maar geen website.",
        ),
        _S.BROKEN: (
            "We probeerden {domain} te bezoeken, maar de site laadde bij ons niet goed.",
            "We kwamen {name} tegen, maar {domain} opende niet goed toen we het probeerden.",
        ),
        _S.PARKED: (
            "We kwamen {name} tegen, maar {domain} toonde alleen een tijdelijke pagina toen we "
            "het openden.",
        ),
        _S.REDIRECT_SOCIAL: (
            "We kwamen {name} tegen en vonden een pagina op sociale media, maar geen eigen "
            "website.",
        ),
        _S.WORKING: ("We kwamen {name} en uw website {domain} tegen.",),
    },
    words=(25, 140),
    judges=(
        r"uw (?:web)?site|uw online aanwezigheid|huidige (?:web)?site|bestaande (?:web)?site|"
        r"verouderd|ontbreekt|recensie|beoordeling|onderscheiding|gegarandeerd|procent"
    ),
)

PL = Phrases(
    name="Polish (formal Państwo)",
    greeting="Szanowny Zespole {name},",
    regards="Z poważaniem,",
    team="Zespół {company}",
    opt_out=(
        "Jeśli nie chcą Państwo otrzymywać od nas wiadomości, wystarczy odpisać „nie, "
        "dziękuję”, a więcej się nie odezwiemy."
    ),
    openings={
        _S.NONE: (
            "Natrafiliśmy na {name} i nie znaleźliśmy strony internetowej firmy.",
            "Szukaliśmy strony internetowej {name}, ale jej nie znaleźliśmy.",
            "Natrafiliśmy na {name}, ale nie na stronę internetową.",
        ),
        _S.BROKEN: (
            "Próbowaliśmy wejść na {domain}, ale strona nie załadowała się prawidłowo.",
            "Natrafiliśmy na {name}, ale {domain} nie otworzyła się prawidłowo, gdy próbowaliśmy.",
        ),
        _S.PARKED: (
            "Natrafiliśmy na {name}, ale pod adresem {domain} wyświetliła się tylko strona "
            "tymczasowa.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Natrafiliśmy na {name} i znaleźliśmy profil w mediach społecznościowych, ale nie "
            "osobną stronę internetową.",
        ),
        _S.WORKING: ("Natrafiliśmy na {name} i Państwa stronę {domain}.",),
    },
    words=(25, 130),
    judges=(
        r"Państwa stron|wasz[aeą]? stron|twoj[aeą]? stron|obecn[aąej]+ stron|"
        r"istniejąc[aąej]+ stron|przestarzał|brakuje Państwu|recenzj|opini[ie] klientów|"
        r"nagrod|gwarant|procent"
    ),
)

RU = Phrases(
    name="Russian (formal вы)",
    greeting="Здравствуйте, команда {name},",
    regards="С уважением,",
    team="Команда {company}",
    opt_out=(
        "Если вы не хотите получать от нас сообщения, просто ответьте «нет, спасибо», и мы "
        "больше не будем вам писать."
    ),
    openings={
        _S.NONE: (
            "Мы узнали о {name} и не нашли сайта компании.",
            "Мы искали сайт {name}, но не нашли его.",
            "Мы узнали о {name}, но сайта не нашли.",
        ),
        _S.BROKEN: (
            "Мы попытались открыть {domain}, но сайт загрузился неправильно.",
            "Мы узнали о {name}, но {domain} не открылся как следует, когда мы попробовали.",
        ),
        _S.PARKED: (
            "Мы узнали о {name}, но по адресу {domain} открылась только страница-заглушка.",
        ),
        _S.REDIRECT_SOCIAL: (
            "Мы узнали о {name} и нашли страницу в социальных сетях, но отдельного сайта не нашли.",
        ),
        _S.WORKING: ("Мы узнали о {name} и о вашем сайте {domain}.",),
    },
    words=(25, 130),
    judges=(
        r"ваш(?:ем|его|им|ей|а)? сайт|онлайн-присутстви|текущ\w* сайт|существующ\w* сайт|"
        r"устаревш|вам не хватает|отзыв|наград|гарант|процент"
    ),
    script="cyrillic",
)

TR = Phrases(
    name="Turkish (formal siz)",
    greeting="Merhaba {name} ekibi,",
    regards="Saygılarımızla,",
    team="{company} ekibi",
    opt_out=(
        "Bizden mesaj almak istemiyorsanız “hayır teşekkürler” yazmanız yeterli; sizinle bir "
        "daha iletişime geçmeyiz."
    ),
    openings={
        _S.NONE: (
            "{name} ile karşılaştık ve işletmeye ait bir web sitesi bulamadık.",
            "{name} için bir web sitesi aradık ancak bulamadık.",
            "{name} ile karşılaştık, ancak bir web sitesi bulamadık.",
        ),
        _S.BROKEN: (
            "{domain} adresini ziyaret etmeyi denedik ancak site düzgün açılmadı.",
            "{name} ile karşılaştık, ancak {domain} denediğimizde düzgün açılmadı.",
        ),
        _S.PARKED: (
            "{name} ile karşılaştık, ancak {domain} açıldığında yalnızca geçici bir sayfa "
            "gösterdi.",
        ),
        _S.REDIRECT_SOCIAL: (
            "{name} ile karşılaştık ve bir sosyal medya sayfası bulduk, ancak ayrı bir web "
            "sitesi bulamadık.",
        ),
        _S.WORKING: ("{name} ile ve {domain} adresindeki web sitenizle karşılaştık.",),
    },
    words=(20, 120),
    judges=(
        r"web siteniz|sitenizi?n?\b|çevrimiçi varlığınız|mevcut site|güncel olmayan|"
        r"eski moda|eksikliğ|müşteri yorum|ödül|garanti|yüzde"
    ),
)

LANGUAGES: dict[str, Phrases] = {
    "en": EN, "ar": AR, "fr": FR, "de": DE, "es": ES, "it": IT, "pt": PT, "nl": NL, "pl": PL,
    "ru": RU, "tr": TR,
}  # fmt: skip

_ARABIC = re.compile(r"[ء-يٮ-ۓ]")
_LATIN = re.compile(r"[A-Za-zÀ-ÿ]")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def lead_language(name: str, country_iso2: str | None) -> str:
    """The language a lead is written to.

    In Arabic-speaking countries many businesses trade in English, so the script of their
    own name decides; elsewhere the country does.
    """
    language = language_for_country(country_iso2)
    if language == "ar" and len(_ARABIC.findall(name)) <= len(_LATIN.findall(name)):
        return "en"
    return language


def display_name(name: str, language: str) -> str:
    """The part of a bilingual name ("Al Noor | النور") in the email's language."""
    wanted, other = (_ARABIC, _LATIN) if language == "ar" else (_LATIN, _ARABIC)
    parts = [p.strip() for p in re.split(r"\s*[|/]\s*|\s+[-–]\s+", name) if p.strip()]
    if len(parts) < 2:
        return name
    own = [p for p in parts if wanted.search(p) and not other.search(p)]
    foreign = [p for p in parts if other.search(p) and not wanted.search(p)]
    return own[0] if own and foreign else name


def readable(text: str | None, language: str) -> str | None:
    """``text`` unless it is in a script the reader of this language would not expect."""
    if not text:
        return None
    for char in text:
        if not char.isalpha() or _LATIN.match(char):
            continue
        if language == "ar" and _ARABIC.match(char):
            continue
        if language == "ru" and _CYRILLIC.match(char):
            continue
        if language != "ar" and ord(char) < 0x250:  # accented Latin letters
            continue
        return None
    return text
