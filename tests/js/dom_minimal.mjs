// Un DOM minimal, en mémoire, pour exécuter le matérialiseur de `web/app/ui.js` sans navigateur.
//
// Ce n'est pas un navigateur et ça ne prétend pas l'être : c'est exactement ce que `materialiser()`,
// `peindre()` et `badgeMode()` touchent — créer un élément, poser du texte, un attribut, une classe,
// brancher un `click`, remonter au `.chips` parent, sélectionner par `#id`, `.classe` ou
// `[data-grp='n']`. Tout le reste lève, plutôt que de rendre `undefined` en silence : un test qui
// passerait parce que le double ne sait pas faire ne prouverait rien.
//
// Aucune dépendance (ni `jsdom` ni autre) : la story 1.7 n'ajoute rien à `pyproject.toml`, et la
// story 1.11 porte le vrai test navigateur. Ce qui se vérifie ici est ce qui se vérifie **sans**
// navigateur : que l'arbre décrit devient un arbre de nœuds, que le texte passe par `textContent`,
// qu'une action décrite devient un bouton cliquable, et que les deux surfaces reçoivent la même
// chose.

class Noeud {
  constructor(tag, doc) {
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = doc;
    this.childNodes = [];
    this.parentElement = null;
    this.attributs = new Map();
    this.ecouteurs = new Map();
    this._texte = "";
    this._value = undefined;
    this.className = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }

  get id() { return this.attributs.get("id") || ""; }
  set id(v) { this.attributs.set("id", String(v)); }

  get classes() { return this.className.split(" ").filter(Boolean); }

  // `value` d'un `<select>`, modélisé comme le navigateur le fait — et c'est **le** point qui
  // compte ici (revue 1.9) : la valeur d'un `<select>` n'existe que tant qu'une `<option>` la
  // porte. Vider la liste puis la reconstruire remet donc la sélection sur la première option, ce
  // qui est exactement le bug qu'un modèle en propriété nue laissait passer — le harnais posait
  // `select.value`, le code reconstruisait les options, et la propriété survivait sans que rien ne
  // le voie. Pour tout autre élément, `value` est la propriété simple d'un champ de saisie.
  get value() {
    if (this.tagName !== "SELECT") return this._value === undefined ? "" : this._value;
    const valeurs = this.childNodes
      .filter((n) => !n.estTexte && n.tagName === "OPTION")
      .map((n) => n.value);
    if (this._value !== undefined && valeurs.indexOf(this._value) !== -1) return this._value;
    return valeurs.length ? valeurs[0] : "";
  }

  set value(v) { this._value = String(v); }

  get textContent() {
    if (!this.childNodes.length) return this._texte;
    return this.childNodes.map((n) => n.textContent).join("");
  }

  set textContent(v) {
    this.childNodes.forEach((n) => { n.parentElement = null; });
    this.childNodes = [];
    this._texte = String(v);
    // Vider un `<select>` retire ses `<option>`, donc **la sélection avec elles** : c'est ce que
    // fait le navigateur, et c'est ce qui rend visible le fait de reconstruire la liste à chaque
    // changement de contrat (revue 1.9). Sans cette ligne, la valeur survivait à sa propre option.
    if (this.tagName === "SELECT") this._value = undefined;
  }

  // Aucun test ne doit pouvoir poser du balisage sans que ça se voie : `innerHTML` existe pour que
  // le vidage de conteneur (le seul usage admis, cf. `test_aucun_innerhtml_ne_sert_a_poser_du_texte`)
  // fonctionne, et lève sur tout le reste.
  set innerHTML(v) {
    if (String(v) !== "") throw new Error("innerHTML non vide : AD-15 interdit ce chemin");
    this.textContent = "";
  }

  get innerHTML() { throw new Error("lecture de innerHTML non modélisée"); }

  appendChild(n) {
    if (this._texte) { // du texte posé puis un enfant ajouté : on garde les deux, dans l'ordre
      const t = this.ownerDocument.createTextNode(this._texte);
      this._texte = "";
      t.parentElement = this;
      this.childNodes.push(t);
    }
    n.parentElement = this;
    this.childNodes.push(n);
    return n;
  }

  remove() {
    const p = this.parentElement;
    if (!p) return;
    p.childNodes = p.childNodes.filter((n) => n !== this);
    this.parentElement = null;
  }

  setAttribute(nom, valeur) { this.attributs.set(String(nom), String(valeur)); }
  getAttribute(nom) { return this.attributs.has(nom) ? this.attributs.get(nom) : null; }
  removeAttribute(nom) { this.attributs.delete(String(nom)); }
  hasAttribute(nom) { return this.attributs.has(String(nom)); }

  addEventListener(type, fn) {
    if (!this.ecouteurs.has(type)) this.ecouteurs.set(type, []);
    this.ecouteurs.get(type).push(fn);
  }

  /** Déclenche un événement, comme le ferait un clic (ou une soumission de formulaire) réel. */
  declencher(type, extra) {
    // `preventDefault` est fourni parce qu'un gestionnaire de `submit` l'appelle toujours : sans
    // lui, le harnais de la page sinistre lèverait sur un chemin que le navigateur emprunte à
    // chaque envoi. Le relevé dit s'il a été appelé — c'est ce qui prouve que la page ne recharge pas.
    const evenement = { type, defautEmpeche: false, preventDefault() { this.defautEmpeche = true; },
                        ...(extra || {}) };
    (this.ecouteurs.get(type) || []).forEach((fn) => fn.call(this, evenement));
    return evenement;
  }

  closest(selecteur) {
    let n = this;
    while (n) {
      if (correspond(n, selecteur)) return n;
      n = n.parentElement;
    }
    return null;
  }

  focus() { this.ownerDocument.actif = this; }

  // `HTMLElement.click()` : le seul moyen, sans navigateur, qu'un gestionnaire posé sur un parent
  // déclenche celui d'un bouton qu'il a trouvé (la carte de clause de la page sinistre le fait).
  click() { this.declencher("click", { target: this }); }

  /** Tous les descendants, en ordre document. */
  descendants() {
    return this.childNodes.flatMap((n) => (n.estTexte ? [n] : [n, ...n.descendants()]));
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }

  querySelectorAll(sel) {
    return this.descendants().filter((n) => !n.estTexte && correspond(n, sel));
  }
}

class NoeudTexte {
  constructor(texte) {
    this.estTexte = true;
    this.textContent = String(texte);
    this.parentElement = null;
  }
}

/** `#id`, `.classe`, `tag`, `[data-x='v']` et leurs concaténations (`.chips[data-grp='3']`). */
function correspond(noeud, selecteur) {
  const parties = String(selecteur).trim().match(/(^[a-zA-Z]+|#[\w-]+|\.[\w-]+|\[[^\]]+\])/g);
  if (!parties) throw new Error(`sélecteur non modélisé : ${selecteur}`);
  return parties.every((p) => {
    if (p.startsWith("#")) return noeud.id === p.slice(1);
    if (p.startsWith(".")) return noeud.classes.indexOf(p.slice(1)) !== -1;
    if (p.startsWith("[")) {
      const m = p.slice(1, -1).match(/^([\w-]+)\s*=\s*'([^']*)'$/);
      if (!m) throw new Error(`sélecteur d'attribut non modélisé : ${p}`);
      return noeud.getAttribute(m[1]) === m[2];
    }
    return noeud.tagName === p.toUpperCase();
  });
}

class Document {
  constructor() {
    this.actif = null;
    this.body = new Noeud("body", this);
    this.body.classList = listeDeClasses(this.body);
  }

  createElement(tag) {
    const n = new Noeud(tag, this);
    n.classList = listeDeClasses(n);
    return n;
  }

  createTextNode(t) { return new NoeudTexte(t); }

  querySelector(sel) { return this.body.querySelector(sel); }
  querySelectorAll(sel) { return this.body.querySelectorAll(sel); }

  // `tools/sinistre/sinistre.js` cherche ses champs par identifiant, comme toute page sans
  // framework. Modélisé sur `querySelector("#id")`, qui l'est déjà.
  getElementById(id) { return this.querySelector("#" + id); }

  // Un gestionnaire posé sur le document (`DOMContentLoaded`) n'est jamais déclenché ici : les
  // harnais chargent le script avec le drapeau « sans démarrage ». La méthode existe pour que le
  // chargement ne lève pas, et elle relève ce qu'on lui a demandé d'écouter.
  addEventListener(type, fn) {
    if (!this.ecouteurs) this.ecouteurs = new Map();
    if (!this.ecouteurs.has(type)) this.ecouteurs.set(type, []);
    this.ecouteurs.get(type).push(fn);
  }

  /** Symétrique d'`addEventListener` : sans elle, ce qui est écouté ici ne peut jamais être joué. */
  declencher(type) {
    const evenement = { type, defautEmpeche: false, preventDefault() { this.defautEmpeche = true; } };
    ((this.ecouteurs && this.ecouteurs.get(type)) || []).forEach((fn) => fn.call(this, evenement));
    return evenement;
  }
}

function listeDeClasses(noeud) {
  return {
    add: (c) => { if (noeud.classes.indexOf(c) === -1) noeud.className = noeud.classes.concat(c).join(" "); },
    remove: (c) => { noeud.className = noeud.classes.filter((x) => x !== c).join(" "); },
    contains: (c) => noeud.classes.indexOf(c) !== -1,
  };
}

/** Un `localStorage` en mémoire — `ui.js` y écrit le profil, jamais l'historique (AD-15). */
function stockage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    entrees: () => Object.fromEntries(m),
  };
}

export { Document, Noeud, stockage, correspond };
