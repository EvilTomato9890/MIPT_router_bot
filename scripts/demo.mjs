import { Store } from '../src/db.mjs';
import { Inventory, place } from '../src/service.mjs';
import { Synchronizer } from '../src/sheets/sync.mjs';
import { MemorySheets } from '../src/sheets/memory.mjs';
const s = new Store(),
  i = new Inventory(s, { bootstrapAdminId: 1, now: () => new Date('2027-06-01T10:00:00+03:00') }),
  a = new MemorySheets(),
  sync = new Synchronizer(i, a);
try {
  const r = i.execute(1, 'demo:add', {
    kind: 'add',
    inventory: 'DEMO-001',
    model: 'Тестовая модель',
    condition: 'working',
    storage: place('Тестовый склад', 'warehouse'),
    kit: { power: true, cable: true, other: '' },
  });
  i.execute(1, 'demo:issue', {
    kind: 'issue',
    routerId: r.routerId,
    recipient: {
      kind: 'person',
      name: 'Тестовый получатель',
      admissionYear: 2026,
      contact: { phone: null, telegram: null, userId: null, raw: '' },
    },
    place: place('Тестовая 101', 'room'),
    dueAt: '2027-07-01',
  });
  console.info('ДЕМОНСТРАЦИЯ — настоящие Telegram и Google не используются.');
  console.info('Синхронизация с тестовым адаптером:', await sync.flush());
  a.sheet.rows[0].values.note = 'Ручная правка тестовой таблицы';
  const p = await sync.preview(1);
  console.info('Изменений таблицы:', p.entries.filter((e) => e.type === 'change').length);
  console.info('Подтверждённый тестовый импорт:', await sync.apply(1, p.id));
  console.info('Статистика:', i.stats(1));
} finally {
  s.close();
}
