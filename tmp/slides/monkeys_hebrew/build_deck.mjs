const fs = await import("node:fs/promises");
const path = await import("node:path");
const { Presentation, PresentationFile } = await import("@oai/artifact-tool");

const W = 1280;
const H = 720;
const ROOT = path.resolve(".");
const DECK_ID = "monkeys_hebrew";
const OUT_DIR = path.join(ROOT, "outputs", DECK_ID);
const REF_DIR = path.join(ROOT, "tmp", "slides", DECK_ID, "pro-reference-images");
const SCRATCH_DIR = path.join(ROOT, "tmp", "slides", DECK_ID);
const PREVIEW_DIR = path.join(SCRATCH_DIR, "preview");
const VERIFY_DIR = path.join(SCRATCH_DIR, "verification");
const INSPECT_PATH = path.join(SCRATCH_DIR, "inspect.ndjson");

const FONT_TITLE = "Arial";
const FONT_BODY = "Arial";
const INK = "#18201B";
const MUTED = "#53615A";
const PAPER = "#FFFDF6";
const PAPER_SOFT = "#FFFDF6E8";
const CREAM = "#F4EBDD";
const GREEN = "#1F6B4A";
const GREEN_DARK = "#143E31";
const MOSS = "#89A257";
const GOLD = "#D8A23E";
const CORAL = "#D96B52";
const SKY = "#DDEBE6";
const TRANSPARENT = "#00000000";

const slides = [
  {
    kicker: "טבע וחקר בעלי חיים",
    title: "קופים: עולם עשיר של חוכמה ותנועה",
    subtitle: "היכרות עם משפחות, מינים, התנהגות ובתי גידול של קופים ברחבי העולם.",
    takeaway: "קופים הם פרימטים חברתיים, סקרנים וגמישים במיוחד.",
    notes: "פתיחה רחבה לנושא: להדגיש שקופים אינם קופי אדם, אך כולם משתייכים לפרימטים.",
  },
  {
    kicker: "מאפיינים",
    title: "מה הופך קוף לקוף?",
    subtitle: "לקופים יש שילוב של גוף מתאים לטיפוס, חושים מפותחים ולמידה חברתית.",
    cards: [
      ["גוף גמיש", "ידיים ורגליים אוחזות, כתפיים ניידות וזנב שעוזר בשיווי משקל אצל מינים רבים."],
      ["ראייה וחישה", "עיניים קדמיות מסייעות להעריך מרחקים בין ענפים, פירות וחברים בקבוצה."],
      ["מוח ולמידה", "קופים לומדים זה מזה: איפה למצוא אוכל, איך לפתור בעיות ואיך להתנהג בקבוצה."],
    ],
    notes: "הדגש המרכזי: קופים מותאמים לחיים בתנועה ובקבוצה.",
  },
  {
    kicker: "חלוקה בסיסית",
    title: "שתי קבוצות מרכזיות",
    subtitle: "בדרך כלל מחלקים קופים לקופי העולם החדש ולקופי העולם הישן.",
    compare: [
      {
        name: "קופי העולם החדש",
        place: "אמריקה המרכזית והדרומית",
        traits: ["לעיתים זנב לופת", "נחיריים פונים לצדדים", "חיים רבים בצמרות יער"],
      },
      {
        name: "קופי העולם הישן",
        place: "אפריקה ואסיה",
        traits: ["לרוב אין זנב לופת", "נחיריים קרובים ופונים מטה", "חלקם חיים גם על הקרקע"],
      },
    ],
    notes: "כאן חשוב לתת כלל אצבע, ולא להפוך את השקופית לשיעור טקסונומי כבד.",
  },
  {
    kicker: "סוגים ודוגמאות",
    title: "מינים מוכרים ומה מייחד אותם",
    subtitle: "כל מין מותאם אחרת לסביבה, למזון ולחיי החברה שלו.",
    species: [
      ["קפוצ'ין", "חכם וסקרן; משתמש בכלים במצבים מסוימים."],
      ["מקאק", "מסתגל היטב לסביבות שונות, כולל אזורים קרובים לאדם."],
      ["מנדריל", "קוף גדול וצבעוני מאפריקה, חי בקבוצות גדולות."],
      ["טמרין", "קטן וזריז, לעיתים עם שפם או ציציות שיער בולטות."],
      ["שאגן", "ידוע בקריאות חזקות שנשמעות למרחק רב ביער."],
      ["חוטמן", "בעל אף ארוך במיוחד, חי באזורי ביצות ונהרות בבורנאו."],
    ],
    notes: "אפשר לבקש מהקהל לבחור איזה מין הכי קל לזכור ולמה.",
  },
  {
    kicker: "התנהגות",
    title: "חיי חברה ותקשורת",
    subtitle: "קופים חיים ברשת חברתית: שיתוף פעולה, משחק, אזהרות ולמידה מצעירים ומבוגרים.",
    cards: [
      ["טיפוח", "סירוק הדדי מנקה פרווה וגם מחזק קשרים בין פרטים."],
      ["קריאות ותנועות", "קולות, הבעות פנים ותנוחות גוף מעבירים אזהרה, פחד, עניין או פיוס."],
      ["משחק ולמידה", "צעירים מתרגלים טיפוס, מרדף ופתרון בעיות דרך משחק."],
    ],
    notes: "להסביר שהחברה של קופים אינה מקרית: יש קשרים, מעמד, הורות ולמידה.",
  },
  {
    kicker: "תזונה ותפקיד בטבע",
    title: "מה קופים אוכלים?",
    subtitle: "התפריט משתנה בין מינים, אבל קופים רבים משפיעים על היער דרך הפצת זרעים.",
    food: [
      ["פירות", "מקור אנרגיה חשוב; זרעים עוברים למקומות חדשים."],
      ["עלים ופרחים", "מזון נפוץ אצל מינים המותאמים לעיכול צמחי."],
      ["חרקים וביצים", "תוספת חלבון במינים אוכלי-כול."],
      ["אגוזים ושורשים", "דורשים כוח, מיומנות או שימוש בחפצים."],
    ],
    notes: "להדגיש שהקוף אינו רק אוכל מהיער; הוא גם משנה ומשמר אותו.",
  },
  {
    kicker: "בתי גידול ושימור",
    title: "איפה הם חיים, ומה מאיים עליהם?",
    subtitle: "קופים חיים ביערות גשם, סוואנות, הרים ומנגרובים, אבל רבים נפגעים מאובדן שטחי מחיה.",
    cards: [
      ["איום מרכזי", "כריתת יערות ופיצול שטחים מקשים על קבוצות למצוא מזון ובני זוג."],
      ["סכנות נוספות", "ציד, סחר לא חוקי בחיות בר וקונפליקט עם בני אדם."],
      ["מה עוזר", "שמורות טבע, מסדרונות אקולוגיים, מחקר וחינוך קהילתי."],
    ],
    notes: "לסיים את החלק הזה בנימה מעשית: שימור עובד כשמשלבים קהילות, מדע ומדיניות.",
  },
  {
    kicker: "סיכום",
    title: "שלוש נקודות לזכור",
    subtitle: "קופים הם לא רק חיות חכמות ומצחיקות; הם חלק חשוב ממערכות טבע מורכבות.",
    summary: [
      "יש קבוצות רבות של קופים, וכל אחת מותאמת לסביבה אחרת.",
      "חיי חברה, תקשורת ולמידה הם חלק מרכזי בהצלחה שלהם.",
      "שמירה על בתי גידול מגינה גם על קופים וגם על היערות שבהם הם חיים.",
    ],
    closing: "שאלה לדיון: איזה קוף הכי מסקרן אתכם לחקור?",
    notes: "סיכום קצר והזמנה לשאלות או להמשך עבודה בכיתה.",
  },
];

const inspectRecords = [];

async function readImageBlob(imagePath) {
  const bytes = await fs.readFile(imagePath);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

async function ensureDirs() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  await fs.mkdir(PREVIEW_DIR, { recursive: true });
  await fs.mkdir(VERIFY_DIR, { recursive: true });
}

function line(fill = TRANSPARENT, width = 0) {
  return { style: "solid", fill, width };
}

function shape(slide, geometry, x, y, w, h, fill = TRANSPARENT, stroke = TRANSPARENT, strokeWidth = 0) {
  return slide.shapes.add({
    geometry,
    position: { left: x, top: y, width: w, height: h },
    fill,
    line: line(stroke, strokeWidth),
  });
}

function addText(slide, slideNo, text, x, y, w, h, opts = {}) {
  const box = shape(slide, "rect", x, y, w, h, opts.fill || TRANSPARENT, opts.stroke || TRANSPARENT, opts.strokeWidth || 0);
  box.text = Array.isArray(text) ? text.join("\n") : text;
  box.text.fontSize = opts.size || 24;
  box.text.color = opts.color || INK;
  box.text.bold = Boolean(opts.bold);
  box.text.typeface = opts.face || FONT_BODY;
  box.text.alignment = opts.align || "right";
  box.text.verticalAlignment = opts.valign || "top";
  box.text.insets = opts.insets || { left: 0, right: 0, top: 0, bottom: 0 };
  if (opts.autoFit) box.text.autoFit = opts.autoFit;
  inspectRecords.push({
    kind: "textbox",
    slide: slideNo,
    role: opts.role || "text",
    text: Array.isArray(text) ? text.join("\n") : text,
    textChars: String(Array.isArray(text) ? text.join("\n") : text).length,
    textLines: String(Array.isArray(text) ? text.join("\n") : text).split(/\n/).length,
    bbox: [x, y, w, h],
  });
  return box;
}

async function addPlate(slide, slideNo) {
  const file = path.join(REF_DIR, `slide-${String(slideNo).padStart(2, "0")}.png`);
  const image = slide.images.add({
    blob: await readImageBlob(file),
    fit: "cover",
    alt: `Text-free monkey visual for slide ${slideNo}`,
  });
  image.position = { left: 0, top: 0, width: W, height: H };
  inspectRecords.push({ kind: "image", slide: slideNo, role: "background visual", path: file, bbox: [0, 0, W, H] });
}

function addTopBar(slide, slideNo, kicker) {
  shape(slide, "rect", 0, 0, W, 68, "#112C25D9");
  addText(slide, slideNo, kicker, 760, 23, 440, 22, {
    size: 15,
    color: "#F8F5EA",
    bold: true,
    role: "section label",
  });
  addText(slide, slideNo, `${slideNo} / ${slides.length}`, 58, 23, 96, 22, {
    size: 14,
    color: "#F8F5EA",
    align: "left",
    role: "slide number",
  });
}

function addTitle(slide, slideNo, title, subtitle, y = 92, width = 710) {
  addText(slide, slideNo, title, W - width - 64, y, width, 72, {
    size: 39,
    color: INK,
    bold: true,
    face: FONT_TITLE,
    role: "title",
    autoFit: "shrinkText",
  });
  addText(slide, slideNo, subtitle, W - width - 64, y + 82, width, 62, {
    size: 22,
    color: MUTED,
    role: "subtitle",
    autoFit: "shrinkText",
  });
  shape(slide, "rect", W - 73, y + 3, 8, 122, GOLD);
}

function addCard(slide, slideNo, x, y, w, h, title, body, accent = GREEN) {
  shape(slide, "roundRect", x, y, w, h, PAPER_SOFT, "#FFFFFF", 1.2);
  shape(slide, "rect", x + w - 10, y + 14, 5, h - 28, accent);
  addText(slide, slideNo, title, x + 22, y + 20, w - 44, 32, {
    size: 21,
    color: GREEN_DARK,
    bold: true,
    role: "card heading",
  });
  addText(slide, slideNo, body, x + 22, y + 64, w - 44, h - 82, {
    size: 17,
    color: INK,
    role: "card body",
    autoFit: "shrinkText",
  });
}

function addPill(slide, slideNo, x, y, w, text, fill, color = "#FFFFFF") {
  shape(slide, "roundRect", x, y, w, 42, fill, TRANSPARENT, 0);
  addText(slide, slideNo, text, x + 16, y + 9, w - 32, 22, {
    size: 17,
    color,
    bold: true,
    align: "center",
    role: "pill label",
  });
}

async function cover(presentation) {
  const data = slides[0];
  const slideNo = 1;
  const slide = presentation.slides.add();
  await addPlate(slide, slideNo);
  shape(slide, "rect", 0, 0, W, H, "#FFFFFFB5");
  shape(slide, "rect", 0, 0, 484, H, "#123B31D9");
  addText(slide, slideNo, data.kicker, 680, 92, 440, 25, { size: 17, color: GREEN_DARK, bold: true, role: "kicker" });
  addText(slide, slideNo, data.title, 565, 138, 610, 145, {
    size: 48,
    color: INK,
    bold: true,
    face: FONT_TITLE,
    role: "cover title",
    autoFit: "shrinkText",
  });
  addText(slide, slideNo, data.subtitle, 640, 310, 535, 76, {
    size: 23,
    color: MUTED,
    role: "cover subtitle",
    autoFit: "shrinkText",
  });
  shape(slide, "roundRect", 673, 448, 502, 94, PAPER_SOFT, "#FFFFFF", 1);
  addText(slide, slideNo, data.takeaway, 704, 475, 440, 38, {
    size: 23,
    color: GREEN_DARK,
    bold: true,
    role: "cover takeaway",
  });
  addPill(slide, slideNo, 76, 580, 300, "מצגת קצרה בעברית", GOLD);
  slide.speakerNotes.setText(data.notes);
}

async function cardsSlide(presentation, idx) {
  const data = slides[idx - 1];
  const slide = presentation.slides.add();
  await addPlate(slide, idx);
  shape(slide, "rect", 0, 0, W, H, "#FFFFFFC9");
  addTopBar(slide, idx, data.kicker);
  addTitle(slide, idx, data.title, data.subtitle);
  const startX = 80;
  const y = 426;
  const cardW = 354;
  const accents = [GREEN, GOLD, CORAL];
  data.cards.forEach(([title, body], i) => addCard(slide, idx, startX + i * 390, y, cardW, 182, title, body, accents[i]));
  slide.speakerNotes.setText(data.notes);
}

async function compareSlide(presentation) {
  const data = slides[2];
  const idx = 3;
  const slide = presentation.slides.add();
  await addPlate(slide, idx);
  shape(slide, "rect", 0, 0, W, H, "#FFFFFFCE");
  addTopBar(slide, idx, data.kicker);
  addTitle(slide, idx, data.title, data.subtitle, 90, 760);
  const panels = [
    { x: 92, fill: "#E6F0E7", accent: GREEN },
    { x: 684, fill: "#F4E8D2", accent: GOLD },
  ];
  data.compare.forEach((item, i) => {
    const p = panels[i];
    shape(slide, "roundRect", p.x, 326, 504, 272, p.fill, "#FFFFFF", 1.4);
    addText(slide, idx, item.name, p.x + 36, 354, 432, 38, { size: 28, color: GREEN_DARK, bold: true, role: "comparison group" });
    addPill(slide, idx, p.x + 204, 404, 264, item.place, p.accent);
    item.traits.forEach((trait, j) => {
      shape(slide, "ellipse", p.x + 448, 466 + j * 42, 14, 14, p.accent);
      addText(slide, idx, trait, p.x + 46, 457 + j * 42, 382, 28, { size: 18, color: INK, role: "comparison trait" });
    });
  });
  slide.speakerNotes.setText(data.notes);
}

async function speciesSlide(presentation) {
  const data = slides[3];
  const idx = 4;
  const slide = presentation.slides.add();
  await addPlate(slide, idx);
  shape(slide, "rect", 0, 0, W, H, "#FFFFFFD4");
  addTopBar(slide, idx, data.kicker);
  addTitle(slide, idx, data.title, data.subtitle, 90, 790);
  const cols = [80, 465, 850];
  const rows = [342, 484];
  data.species.forEach(([name, detail], i) => {
    const x = cols[i % 3];
    const y = rows[Math.floor(i / 3)];
    const accent = [GREEN, GOLD, CORAL, MOSS, GREEN_DARK, "#7F6E9A"][i];
    shape(slide, "roundRect", x, y, 344, 108, PAPER_SOFT, "#FFFFFF", 1);
    shape(slide, "ellipse", x + 286, y + 24, 40, 40, accent);
    addText(slide, idx, name, x + 24, y + 19, 244, 30, { size: 23, color: GREEN_DARK, bold: true, role: "species name" });
    addText(slide, idx, detail, x + 24, y + 56, 286, 36, { size: 15, color: INK, role: "species detail", autoFit: "shrinkText" });
  });
  slide.speakerNotes.setText(data.notes);
}

async function foodSlide(presentation) {
  const data = slides[5];
  const idx = 6;
  const slide = presentation.slides.add();
  await addPlate(slide, idx);
  shape(slide, "rect", 0, 0, W, H, "#FFFFFFC8");
  addTopBar(slide, idx, data.kicker);
  addTitle(slide, idx, data.title, data.subtitle, 90, 780);
  const centerX = 640;
  const centerY = 490;
  shape(slide, "ellipse", centerX - 88, centerY - 88, 176, 176, "#E6F0E7", GREEN, 2);
  addText(slide, idx, "תפריט\nמשתנה", centerX - 62, centerY - 42, 124, 82, {
    size: 27,
    color: GREEN_DARK,
    bold: true,
    align: "center",
    valign: "middle",
    role: "food center",
  });
  const positions = [
    [142, 374, GREEN],
    [760, 374, GOLD],
    [142, 538, CORAL],
    [760, 538, MOSS],
  ];
  data.food.forEach(([name, detail], i) => {
    const [x, y, accent] = positions[i];
    addCard(slide, idx, x, y, 360, 118, name, detail, accent);
  });
  slide.speakerNotes.setText(data.notes);
}

async function summarySlide(presentation) {
  const data = slides[7];
  const idx = 8;
  const slide = presentation.slides.add();
  await addPlate(slide, idx);
  shape(slide, "rect", 0, 0, W, H, "#FFFFFFC4");
  addTopBar(slide, idx, data.kicker);
  addTitle(slide, idx, data.title, data.subtitle, 96, 790);
  data.summary.forEach((item, i) => {
    const y = 352 + i * 76;
    shape(slide, "ellipse", 1052, y - 4, 44, 44, [GREEN, GOLD, CORAL][i]);
    addText(slide, idx, String(i + 1), 1062, y + 6, 24, 22, { size: 19, color: "#FFFFFF", bold: true, align: "center", role: "summary number" });
    addText(slide, idx, item, 236, y, 780, 42, { size: 23, color: INK, role: "summary point", autoFit: "shrinkText" });
  });
  shape(slide, "roundRect", 230, 614, 820, 56, "#143E31E8", TRANSPARENT, 0);
  addText(slide, idx, data.closing, 260, 628, 760, 26, { size: 22, color: "#FFFFFF", bold: true, align: "center", role: "closing question" });
  slide.speakerNotes.setText(data.notes);
}

async function build() {
  await ensureDirs();
  const presentation = Presentation.create({ slideSize: { width: W, height: H } });
  await cover(presentation);
  await cardsSlide(presentation, 2);
  await compareSlide(presentation);
  await speciesSlide(presentation);
  await cardsSlide(presentation, 5);
  await foodSlide(presentation);
  await cardsSlide(presentation, 7);
  await summarySlide(presentation);
  return presentation;
}

async function saveBlob(blob, filePath) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  await fs.writeFile(filePath, bytes);
}

async function verifyAndExport(presentation) {
  inspectRecords.unshift({ kind: "deck", id: DECK_ID, slideCount: presentation.slides.count, slideSize: { width: W, height: H } });
  await fs.writeFile(INSPECT_PATH, inspectRecords.map((r) => JSON.stringify(r)).join("\n") + "\n", "utf8");
  const previewPaths = [];
  for (let i = 0; i < presentation.slides.items.length; i++) {
    const preview = await presentation.export({ slide: presentation.slides.items[i], format: "png", scale: 1 });
    const previewPath = path.join(PREVIEW_DIR, `slide-${String(i + 1).padStart(2, "0")}.png`);
    await saveBlob(preview, previewPath);
    previewPaths.push(previewPath);
  }
  const outPath = path.join(OUT_DIR, "output.pptx");
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(outPath);
  await fs.writeFile(
    path.join(VERIFY_DIR, "render_verify_loops.ndjson"),
    JSON.stringify({ kind: "render_verify_loop", loop: 1, slideCount: presentation.slides.count, previewPaths, pptxPath: outPath, timestamp: new Date().toISOString() }) + "\n",
    "utf8",
  );
  return outPath;
}

const presentation = await build();
const outPath = await verifyAndExport(presentation);
console.log(outPath);
