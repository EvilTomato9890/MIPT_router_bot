import { Store } from '../src/db.mjs';
import { Inventory, place } from '../src/service.mjs';
import { MemorySheets } from '../src/sheets/memory.mjs';
import { Synchronizer } from '../src/sheets/sync.mjs';
import { ColorRepair } from '../src/sheets/colors.mjs';
import { Campaigns } from '../src/campaigns.mjs';
let seq = 0;
export const key = () => `test:${++seq}`;
export const person = (name = 'Тестовый получатель') => ({
  kind: 'person',
  name,
  admissionYear: 2026,
  group: 'ТЕСТ-01',
  contact: { raw: '', phone: null, telegram: null, userId: null },
});
export function setup(t, options = {}) {
  const s = new Store(options.path),
    clock = { at: new Date('2027-06-01T10:00:00+03:00') },
    i = new Inventory(s, { bootstrapAdminId: 1, now: () => new Date(clock.at) }),
    a = options.adapter || new MemorySheets(),
    sync = new Synchronizer(i, a),
    colors = new ColorRepair(i, a),
    c = new Campaigns(i);
  t?.after(() => s.close());
  i.setRole(1, 2, 'operator', key());
  i.setRole(1, 3, 'observer', key());
  return { s, i, a, sync, colors, c, clock };
}
export function add(i, inventory = 'TEST-01', more = {}) {
  return i.execute(1, key(), {
    kind: 'add',
    inventory,
    condition: 'working',
    storage: place('Тестовый склад', 'warehouse'),
    kit: { power: true, cable: true, other: '' },
    ...more,
  }).routerId;
}
export function issue(i, id, more = {}) {
  return i.execute(2, key(), {
    kind: 'issue',
    routerId: id,
    recipient: person(),
    place: place('101', 'room', 'confirmed', '1', 'А'),
    dueAt: '2027-07-01',
    ...more,
  });
}
export function receive(i, id, more = {}) {
  return i.execute(2, key(), {
    kind: 'return',
    routerId: id,
    inventory: i.get(2, id).inventory,
    physical: true,
    storage: place('Тестовый склад', 'warehouse'),
    condition: 'working',
    kit: { power: true, cable: true, other: '' },
    ...more,
  });
}
export async function campaign(i, c, id, more = {}) {
  const saved = c.save(1, key(), {
    name: 'Тестовый сбор',
    admissionYear: 2026,
    startsAt: '2027-05-01',
    deadline: '2027-07-01',
    responsibleIds: [2],
    location: 'Тестовый склад',
    hours: '10–18',
    instruction: 'Сдать роутер с блоком питания',
    ...more,
  });
  const p = c.preview(1, saved.id);
  c.launch(1, key(), saved.id, p.campaign.version);
  return saved.id;
}
