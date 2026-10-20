"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { parseFontFaces, groupFontFamilies } = require("./system-fonts");

function sfnt({ family = "Fixture Family", style = "Regular", postscriptName = "Fixture-Regular", weight = 400, italic = false, fixed = false, widths = [600, 300] } = {}, base = 0) {
  const values = new Map([[1, family], [2, style], [4, `${family} ${style}`], [6, postscriptName], [16, family], [17, style]]);
  const encoded = [...values.values()].map(text => Buffer.from(text, "utf16le").swap16());
  const name = Buffer.alloc(6 + values.size * 12 + encoded.reduce((sum, item) => sum + item.length, 0));
  name.writeUInt16BE(values.size, 2); name.writeUInt16BE(6 + values.size * 12, 4);
  let stringOffset = 0;
  [...values.keys()].forEach((id, index) => {
    const at = 6 + index * 12;
    [3, 1, 0x0409, id, encoded[index].length, stringOffset].forEach((value, field) => name.writeUInt16BE(value, at + field * 2));
    encoded[index].copy(name, 6 + values.size * 12 + stringOffset); stringOffset += encoded[index].length;
  });
  const os2 = Buffer.alloc(64); os2.writeUInt16BE(weight, 4); os2.writeUInt16BE(italic ? 1 : 0, 62);
  const post = Buffer.alloc(16); post.writeUInt32BE(fixed ? 1 : 0, 12);
  const hhea = Buffer.alloc(36); hhea.writeUInt16BE(widths.length, 34);
  const hmtx = Buffer.alloc(widths.length * 4); widths.forEach((width, index) => hmtx.writeUInt16BE(width, index * 4));
  const head = Buffer.alloc(48);
  const tables = new Map([["name", name], ["OS/2", os2], ["post", post], ["hhea", hhea], ["hmtx", hmtx], ["head", head]]);
  const buffer = Buffer.alloc(12 + tables.size * 16 + [...tables.values()].reduce((sum, table) => sum + table.length, 0));
  buffer.writeUInt32BE(0x10000, 0); buffer.writeUInt16BE(tables.size, 4);
  let offset = 12 + tables.size * 16;
  [...tables].forEach(([tag, data], index) => {
    const at = 12 + index * 16; buffer.write(tag, at, "ascii"); buffer.writeUInt32BE(base + offset, at + 8); buffer.writeUInt32BE(data.length, at + 12);
    data.copy(buffer, offset); offset += data.length;
  });
  return buffer;
}

test("reads actual registered style names, full names, PostScript names and metrics", () => {
  const [face] = parseFontFaces(sfnt({ family: "Fixture 微软", style: "Condensed Medium Italic", postscriptName: "Fixture-CondensedMediumItalic", weight: 550, italic: true }));
  assert.deepEqual(face, { family: "Fixture 微软", fullName: "Fixture 微软 Condensed Medium Italic", postscriptName: "Fixture-CondensedMediumItalic",
    styleName: "Condensed Medium Italic", weight: 550, style: "italic", isMonospaced: false });
});

test("monospace metadata and equal positive metrics are consumed without guessing by family name", () => {
  assert.equal(parseFontFaces(sfnt({ family: "Not named mono", fixed: true }))[0].isMonospaced, true);
  assert.equal(parseFontFaces(sfnt({ widths: [600, 600, 0] }))[0].isMonospaced, true);
  assert.equal(parseFontFaces(sfnt({ family: "Fake Mono", widths: [600, 350] }))[0].isMonospaced, false);
});

test("font collections retain each face and choose an actual regular face before narrow variants", () => {
  const first = sfnt({ style: "Narrow", postscriptName: "Fixture-Narrow" }, 20);
  const second = sfnt({ style: "Regular", postscriptName: "Fixture-Regular" }, 20 + first.length);
  const header = Buffer.alloc(20); header.write("ttcf"); header.writeUInt32BE(0x10000, 4); header.writeUInt32BE(2, 8);
  header.writeUInt32BE(20, 12); header.writeUInt32BE(20 + first.length, 16);
  const faces = parseFontFaces(Buffer.concat([header, first, second]));
  const families = groupFontFamilies([...faces, ...faces]);
  assert.equal(families.length, 1); assert.equal(families[0].faces.length, 2);
  assert.deepEqual(families[0].faces.map(face => face.styleName), ["Regular", "Narrow"]);
});

test("unsupported bitmap files are omitted and corrupt SFNT offsets retain a real error", () => {
  assert.deepEqual(parseFontFaces(Buffer.from("MZ old bitmap font")), []);
  const corrupt = sfnt(); corrupt.writeUInt32BE(corrupt.length + 1, 20);
  assert.throws(() => parseFontFaces(corrupt), /Invalid name font table/);
});
