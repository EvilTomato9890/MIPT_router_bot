import test from 'node:test';
import assert from 'node:assert/strict';
import { Bot } from '../src/bot.mjs';
import { Telegram } from '../src/telegram.mjs';
import { fields } from '../src/dialogs.mjs';
import { setup, key, add, issue, receive, campaign } from './helpers.mjs';
let update = 100;
class FakeTelegram {
  messages = [];
  async send(id, text, markup) {
    for (const row of markup?.inline_keyboard || [])
      for (const b of row)
        assert.ok(
          Buffer.byteLength(b.callback_data, 'utf8') <= 64,
          `Слишком длинная кнопка ${b.callback_data}`,
        );
    this.messages.push({ id, text, markup });
    return { message_id: this.messages.length };
  }
  async answer(id, text = '') {
    this.lastAnswer = text;
  }
  async file(id, name, content) {
    this.lastFile = { id, name, content };
  }
}
function botSetup(t) {
  const ctx = setup(t),
    telegram = new FakeTelegram(),
    bot = new Bot(ctx.i, telegram, {
      sync: ctx.sync,
      colors: ctx.colors,
      campaigns: ctx.c,
      mode: 'memory',
    });
  return { ...ctx, telegram, bot };
}
const msg = (id, text, n = ++update) => ({
  update_id: n,
  message: { from: { id }, chat: { id, type: 'private' }, text },
});
const cb = (id, data, n = ++update) => ({
  update_id: n,
  callback_query: {
    id: 'query' + n,
    from: { id },
    data,
    message: { chat: { id, type: 'private' } },
  },
});
const finalButton = (telegram) =>
  telegram.messages
    .at(-1)
    .markup.inline_keyboard.flat()
    .find((b) => b.callback_data.startsWith('confirm:')).callback_data;
test('01b. Неавторизованный и групповой чат не получают данные', async (t) => {
  const { i, bot, telegram } = botSetup(t),
    id = add(i, 'СЕКРЕТНЫЙ-НОМЕР');
  await bot.handle(cb(99, 'card:' + id));
  assert.ok(!JSON.stringify(telegram.messages).includes('СЕКРЕТНЫЙ'));
  assert.match(telegram.messages.at(-1).text, /Нет доступа/);
  const before = telegram.messages.length;
  await bot.handle({
    update_id: ++update,
    message: { from: { id: 1 }, chat: { id: -10, type: 'group' }, text: '/list' },
  });
  assert.equal(telegram.messages.length, before);
});
test('01c. Отзыв прав в открытом диалоге и старые административные кнопки', async (t) => {
  const { i, bot, telegram, colors, sync } = botSetup(t),
    id = add(i);
  await bot.start(2, 'lost', { routerId: id, expected: { [id]: 1 } });
  await bot.answerWizard(2, 'Тестовая причина');
  const confirmation = finalButton(telegram);
  i.setRole(1, 2, null, key());
  await bot.handle(cb(2, confirmation));
  assert.equal(i.get(1, id).status, 'stored');
  assert.match(telegram.messages.at(-1).text, /Нет доступа/);
  i.setRole(1, 2, 'admin', key());
  const p = await sync.preview(2),
    color = await colors.preview(2);
  i.setRole(1, 2, 'observer', key());
  await bot.handle(cb(2, 'sa:' + p.id));
  assert.match(telegram.messages.at(-1).text, /Нет доступа/);
  await bot.handle(cb(2, 'confirm:' + color.id));
  assert.match(telegram.messages.at(-1).text, /Нет доступа/);
});
test('Диалог добавления: назад, отмена, итог, повторный update и повтор подтверждения', async (t) => {
  const { i, bot, telegram } = botSetup(t);
  await bot.start(2, 'add');
  const initial = msg(2, 'DIALOG-1');
  await bot.handle(initial);
  await bot.handle(initial);
  assert.equal(bot._session(2).step, 1);
  await bot.back(2);
  assert.equal(bot._session(2).step, 0);
  await bot.handle(msg(2, 'DIALOG-1'));
  const answers = {
    model: '-',
    serial: '-',
    mac: '-',
    condition: 'working',
    storageName: 'Тестовый склад',
    storageType: 'warehouse',
    power: 'yes',
    cable: 'no',
    other: '-',
  };
  while (bot._session(2)) {
    const s = bot._session(2),
      f = fields(s.kind, s.data)[s.step];
    await bot.handle(msg(2, answers[f.key]));
  }
  assert.equal(i.list(1).length, 0);
  const confirm = finalButton(telegram);
  await bot.handle(cb(2, confirm));
  await bot.handle(cb(2, confirm));
  assert.equal(i.list(1).length, 1);
  assert.equal(i.history(1, i.list(1)[0].id).length, 1);
  assert.ok(telegram.messages.some((m) => m.text.includes('Инвентарный номер: DIALOG-1')));
  await bot.start(2, 'add');
  await bot.handle(msg(2, 'CANCELLED'));
  await bot.handle(msg(2, '/cancel'));
  assert.equal(i.list(1).length, 1);
});
test('Кнопка шага привязана к диалогу; подтверждение сверяет версию карточки', async (t) => {
  const { i, bot, telegram } = botSetup(t),
    id = add(i);
  await bot.start(1, 'vip', { routerId: id, expected: { [id]: 1 } });
  const old = telegram.messages.at(-1).markup.inline_keyboard[0][0].callback_data;
  await bot.start(1, 'vip', { routerId: id, expected: { [id]: 1 } });
  await bot.handle(cb(1, old));
  assert.equal(bot._session(1).step, 0);
  assert.match(telegram.messages.at(-1).text, /устарела/);
  await bot.answerWizard(1, 'yes');
  await bot.answerWizard(1, 'Тестовый смысл');
  const confirm = finalButton(telegram);
  i.execute(2, key(), { kind: 'note', routerId: id, note: 'Параллельная правка' });
  await bot.handle(cb(1, confirm));
  assert.equal(i.get(1, id).vip, false);
  assert.match(telegram.messages.at(-1).text, /Карточка изменилась/);
});
test('Просмотр, пагинация, фильтры, экспорт и кнопки кампании укладываются в Bot API', async (t) => {
  const { i, bot, telegram, c } = botSetup(t);
  for (let n = 0; n < 8; n++) add(i, 'P-' + n);
  await bot.showList(3, {}, 0);
  assert.match(telegram.messages.at(-1).text, /Найдено: 8/);
  await bot.reports(3);
  await bot.export(3);
  assert.ok(telegram.lastFile.content.startsWith('\uFEFF'));
  await assert.rejects(() => bot.settings(3), /Нет доступа/);
  const id = i.list(1)[0].id;
  issue(i, id);
  const cid = await campaign(i, c);
  await bot.campaignPreview(1, cid);
  await bot.queue(2, cid, 0);
  await bot.card(1, id);
  await bot.history(3, id, 0);
  assert.ok(telegram.messages.length > 5);
});
test('Старая кнопка очереди не обращается к другому роутеру после возврата', async (t) => {
  const { i, bot, telegram, c } = botSetup(t),
    one = add(i, 'Q1'),
    two = add(i, 'Q2');
  issue(i, one);
  issue(i, two);
  const cid = await campaign(i, c);
  await bot.queue(2, cid, 0);
  const action = telegram.messages
    .at(-1)
    .markup.inline_keyboard.flat()
    .find((b) => b.callback_data.startsWith('qa:')).callback_data;
  receive(i, one);
  await bot.handle(cb(2, action));
  assert.match(telegram.messages.at(-1).text, /Очередь изменилась/);
  assert.equal(bot._session(2), null);
});
test('Telegram ошибки не раскрывают токен, контакты или ответ сервера', async () => {
  const token = '123:TEST_SECRET',
    api = new Telegram(token, async () => {
      throw new Error('https://api.telegram.org/bot' + token + '/sendMessage');
    });
  await assert.rejects(
    () => api.send(1, 'private'),
    (e) => !e.message.includes(token),
  );
  const denied = new Telegram(token, async () => ({
    json: async () => ({ ok: false, error_code: 400, description: 'ЧУЖИЕ ДАННЫЕ' }),
  }));
  await assert.rejects(
    () => denied.send(1, 'private'),
    (e) => !e.message.includes('ЧУЖИЕ'),
  );
});
