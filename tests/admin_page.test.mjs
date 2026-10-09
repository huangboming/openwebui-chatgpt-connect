import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(process.argv[2], "utf8");

class Element {
  children = [];
  listeners = new Map();
  attributes = new Map();
  disabled = false;
  checked = false;
  classes = new Set();
  classList = {
    toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
  };

  replaceChildren(...children) {
    this.children = children;
  }

  querySelectorAll(selector) {
    assert.equal(selector, 'input[type="checkbox"]');
    const descendants = element => element.children.flatMap(child => [child, ...descendants(child)]);
    return descendants(this).filter(element => element.type === "checkbox");
  }

  append(...children) {
    this.children.push(...children);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  dispatch(type) {
    return this.listeners.get(type)?.({ target: this });
  }

  click() {
    if (!this.disabled) return this.dispatch("click");
  }
}

function createDocument() {
  const entities = { "&quot;": '"', "&#x27;": "'", "&amp;": "&", "&lt;": "<", "&gt;": ">" };
  const meta = new Map([...html.matchAll(/<meta name="([^"]+)" content="([^"]*)">/g)].map(
    ([, name, content]) => [name, {
      content: content.replace(/&quot;|&#x27;|&amp;|&lt;|&gt;/g, entity => entities[entity]),
    }]
  ));
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(
    ([, id]) => [id, new Element()]
  ));
  return {
    getElementById: id => elements.get(id),
    createElement: () => new Element(),
    querySelector: selector => meta.get(selector.match(/meta\[name="([^"]+)"\]/)[1]),
    querySelectorAll: selector => elements.get("model-list").querySelectorAll(selector),
  };
}

test("edits made during a save remain unsaved and cannot start another concurrent save", async () => {
  const document = createDocument();
  const saves = [];
  const response = data => ({ ok: true, status: 200, json: async () => data });
  const context = vm.createContext({
    document,
    location: { protocol: "https:" },
    setTimeout,
    clearTimeout,
    fetch: async (url, options) => {
      if (url === "/siwc/admin/models" && options.method === "POST") {
        return new Promise(resolve => saves.push({ enabled: JSON.parse(options.body).enabled, resolve }));
      }
      if (url === "/siwc/admin/models") {
        return response({ models: ["first-model", "second-model"].map(id => ({
          id, name: id, enabled: true, manageable: true, synchronized: true, reasoning_efforts: [],
        })) });
      }
      assert.equal(url, "/api/models?refresh=true");
      return response({});
    },
  });
  vm.runInContext(html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1], context);
  await new Promise(setImmediate);

  const [first, second] = document.querySelectorAll('input[type="checkbox"]');
  const save = document.getElementById("save-models");
  assert.ok(save.disabled);
  second.checked = false;
  second.dispatch("change");
  assert.equal(save.disabled, false);
  const pending = save.click();
  assert.deepEqual(saves[0].enabled, ["first-model"]);

  second.checked = true;
  second.dispatch("change");
  first.checked = false;
  first.dispatch("change");
  assert.ok(save.disabled);
  save.click();
  assert.equal(saves.length, 1);
  saves[0].resolve(response({ enabled: 1, custom_avatars_preserved: 0 }));
  await pending;
  assert.equal(save.disabled, false);

  const next = save.click();
  assert.deepEqual(saves[1].enabled, ["second-model"]);
  saves[1].resolve(response({ enabled: 1, custom_avatars_preserved: 0 }));
  await next;
  assert.ok(save.disabled);
});
