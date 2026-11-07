import { parentPort, workerData } from 'node:worker_threads';
import { Store } from '../../src/db.mjs';
import { Inventory, place } from '../../src/service.mjs';
const { path, id, actor, gate } = workerData,
  s = new Store(path),
  i = new Inventory(s, { now: () => new Date('2027-06-01T10:00:00Z') });
parentPort.postMessage({ ready: true });
Atomics.wait(new Int32Array(gate), 0, 0);
try {
  i.execute(actor, 'race:' + actor, {
    kind: 'issue',
    routerId: id,
    recipient: { kind: 'person', name: 'Тестовый ' + actor },
    place: place('101', 'room'),
    dueAt: '2027-07-01',
  });
  parentPort.postMessage({ ok: true });
} catch (e) {
  parentPort.postMessage({ ok: false, error: e.message });
} finally {
  s.close();
}
