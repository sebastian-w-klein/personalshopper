// Run: node --test tests/
// Synthetic page text modelled on a TRR product page. If TRR's layout differs,
// paste real lines from the userscript's "Grab this page" debug into a new case.
const test = require('node:test');
const assert = require('node:assert');
const p = require('../userscript/trr-suit-scout.user.js');

const LINES = [
  'Holiday Shipping Guidelines',
  'Final Sale',                       // nav link: must be ignored (outside product region)
  'Gucci',
  'Gucci Wool Two-Piece Suit',
  '$650',
  '$495',
  '24% Off',
  'Est. Retail $3,200',
  'Size: 50',
  'Add to Bag',
  'Details',
  'Gucci Suit',
  'Black Wool',
  '100% Wool',
  'Notch Lapels & Two Button Closure',
  'Estimated item measurements:',
  'Chest: 21"',
  'Shoulder: 18.5"',
  'Sleeve: 25 1/2"',
  'Length: 30"',
  'Waist: 33"',
  'Inseam: 31"',
  'Condition: Very Good. Light wear at cuffs; minor pilling throughout.',
  'Item #: GUC1234567',
  'You May Also Like',
  'Brioni Suit $1,200',
];

test('parses core fields', () => {
  const r = p.parseProductLines(LINES, { h1: 'Gucci Wool Two-Piece Suit', designer: 'Gucci', url: 'https://www.therealreal.com/products/men/clothing/suiting/gucci-wool-two-piece-suit-vjyeb' });
  assert.strictEqual(r.price, 495);
  assert.strictEqual(r.original_price, 650);
  assert.strictEqual(r.est_retail, 3200);
  assert.strictEqual(r.size_label, '50');
  assert.strictEqual(r.item_id, 'GUC1234567');
  assert.strictEqual(r.condition, 'Very Good');
  assert.match(r.condition_notes, /pilling/);
  assert.strictEqual(r.final_sale, false);
  assert.deepStrictEqual(r.measurements, { chest: 21, shoulder: 18.5, sleeve: 25.5, length: 30, waist: 33, inseam: 31 });
  assert.ok(r.fabrics.includes('wool'));
  assert.deepStrictEqual(r.composition, [{ pct: 100, fiber: 'wool' }]);
});

test('final sale inside product region is detected', () => {
  const lines = LINES.slice(); lines.splice(9, 0, 'This item is Final Sale');
  assert.strictEqual(p.parseProductLines(lines, { h1: 'Gucci Wool Two-Piece Suit' }).final_sale, true);
});

test('meta price wins, single price without discount', () => {
  const lines = ['Kiton Suit', '$900', 'Est. Retail $7,000'];
  const r = p.parseProductLines(lines, { h1: 'Kiton Suit', metaPrice: '850.00' });
  assert.strictEqual(r.price, 850);
  assert.strictEqual(r.original_price, null);
});

test('label/value on separate lines', () => {
  const lines = ['X Suit', '$300', 'Estimated Item Measurements', 'Chest', '42"', 'Sleeve', '24.5"', 'Condition: Excellent'];
  const r = p.parseProductLines(lines, { h1: 'X Suit' });
  assert.deepStrictEqual(r.measurements, { chest: 42, sleeve: 24.5 });
  assert.strictEqual(r.condition, 'Excellent');
});

test('mergeItem appends price history only on change', () => {
  let it = p.mergeItem(null, { price: 500, url: 'u' }, 't1');
  it = p.mergeItem(it, { price: 500, url: 'u' }, 't2');
  it = p.mergeItem(it, { price: 425, url: 'u' }, 't3');
  assert.deepStrictEqual(it.price_history, [{ t: 't1', price: 500 }, { t: 't3', price: 425 }]);
  assert.strictEqual(it.first_seen, 't1');
});

test('itemKey falls back to slug code', () => {
  assert.strictEqual(p.itemKey({ url: 'https://x/products/men/clothing/suiting/gucci-wool-two-piece-suit-vjyeb' }), 'SLUG-vjyeb');
});
