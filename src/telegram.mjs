import { DomainError } from './util.mjs';

export class Telegram {
  constructor(token, request = fetch) {
    this.token = token;
    this.fetch = request;
  }
  async call(method, data = {}, timeout = 40000) {
    let response;
    try {
      response = await this.fetch(`https://api.telegram.org/bot${this.token}/${method}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(data),
        signal: AbortSignal.timeout(timeout),
      });
    } catch {
      throw new DomainError('Telegram: доставка не подтверждена', 'network');
    }
    let body;
    try {
      body = await response.json();
    } catch {
      throw new DomainError('Telegram: неверный ответ', 'network');
    }
    if (!body.ok)
      throw new DomainError(
        body.error_code === 429 ? 'Telegram: превышен лимит запросов' : 'Telegram: запрос отклонён',
        'network',
      );
    return body.result;
  }
  send(chat, text, keyboard) {
    return this.call('sendMessage', {
      chat_id: chat,
      text: String(text).slice(0, 4000),
      ...(keyboard ? { reply_markup: keyboard } : {}),
    });
  }
  answer(id, text = '') {
    return this.call('answerCallbackQuery', { callback_query_id: id, text });
  }
  async file(chat, name, content) {
    const form = new FormData();
    form.set('chat_id', String(chat));
    form.set('document', new Blob([content], { type: 'text/csv;charset=utf-8' }), name);
    let r;
    try {
      r = await this.fetch(`https://api.telegram.org/bot${this.token}/sendDocument`, {
        method: 'POST',
        body: form,
        signal: AbortSignal.timeout(30000),
      });
      if (!(await r.json()).ok) throw new Error();
    } catch {
      throw new DomainError('Telegram: отправка файла не подтверждена', 'network');
    }
  }
}
