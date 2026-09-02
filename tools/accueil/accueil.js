// L'accueil du retour (story 1.10) — HTML/CSS/JS vanilla, sans build, sans framework, sans requête
// tierce, sans `localStorage`.
//
// Même partage qu'en 1.7 et 1.9, et pour la même raison : **ce qui décide est séparé de ce qui
// peint**. `libelleValidation`, `vueEtat`, `vueAlertes` et `vueSondeEchouee` sont **pures** — elles
// rendent un arbre `{tag, cls, texte, enfants}` et ne touchent ni au DOM ni au réseau ;
// `materialiser()` transforme cet arbre en DOM et ne décide de rien, en posant tout texte par
// `textContent` (AD-15). C'est ce qui rend la promesse de la story vérifiable sans navigateur : « la
// page n'invente aucun niveau quand la sonde échoue » est une assertion sur un arbre.
//
// L'arbre est exactement `{tag, cls, texte, enfants}` — **pas** de `href` : tous les liens de cette
// page sont statiques (les trois entrées, le pied de page) et vivent dans `index.html`. Le
// matérialiseur en portait la branche, que `noeud()` ne pouvait pas produire : du code que rien ne
// pouvait atteindre, et un en-tête qui décrivait un contrat plus large que le vrai (revue 1.10).
//
// AD-11 / AD-16 / D8 : **trois états, et trois seulement**.
//   1. la sonde répond avec un profil  → « niveau de validation : vertical — N cas relus à la main »
//      (ou « … relus par la boucle, contresignature humaine en attente » tant que le serveur publie
//      `gate_countersigned: false` — la qualification humaine vient du gate, jamais du nom du profil)
//   2. la sonde répond sans profil     → « aucun gate » + les alertes qui disent pourquoi
//   3. la sonde ne répond pas          → « inconnu — le serveur n'a pas répondu », et **rien d'autre**
// Le troisième n'affiche jamais le dernier profil connu ni un défaut optimiste : c'est le même
// interdit qu'AD-16 pose au front du sinistre. Aucun compte de cas n'est écrit dans ce fichier :
// `gate_cases` vient du serveur, qui le tient du run qui l'a constaté (AD-7).
//
// AD-5 (story 2.1) : la page dit aussi où en est le **dictionnaire des variantes**, en quatre
// formulations — armé, chargé mais non signé, d'un autre corpus, absent. Ce qu'elle en annonce, c'est
// le sort du refus « zéro hit », et elle le lit sur `dictionary.refus_zero_hit_actif` : la règle qui
// combine les deux verrous n'a qu'une autorité, le serveur (`api/schemas.EtatDictionnaire`). Aucun
// nombre, aucune conjonction refaite ici.

(function () {
  "use strict";

  // AD-12 : une seule origine. Le serveur qui sert cette page sert aussi l'API — aucune URL en dur,
  // aucun CORS. Une page ouverte en `file://` n'a aucun serveur à sonder : c'est l'état 3.
  var API_BASE = (window.location && /^https?:$/.test(window.location.protocol))
    ? window.location.origin : "";

  // Budget **total** de la sonde, en secondes. `/api/v1/sante` ne coûte rien et n'est pas limitée,
  // mais une sonde qui pend laisserait le bloc « état du système » vide sans un mot.
  //
  // Ce n'est pas une marge ajoutée à une deadline serveur — il n'y a pas de deadline serveur sur
  // `/sante`, tout y est calculé au démarrage. C'est le temps qu'on accepte d'attendre pour un état
  // de santé, et la valeur choisie est celle que `config.py` réserve déjà au client
  // (`client_abort_margin_s`) : la page ne peut pas lire un seuil sur la sonde qu'elle est en train
  // de borner, et en recopier un autre le ferait diverger de `config.py` (convention Seuils). Un
  // test l'amarre à `Settings().thresholds()["client_abort_margin_s"]`.
  var SONDE_BUDGET_S = 10;

  // Les deux routes que cette page sonde, et rien d'autre. Toutes deux sont calculées au démarrage
  // du serveur, ne coûtent rien et ne sont pas limitées (AD-13 protège les routes qui appellent un
  // modèle). `/api/v1/evals/latest` publie le **dernier run d'évals** — rouge compris (FR41) : la
  // page reprend le même artefact que `docs/evals/latest.md` et que le résumé de CI, sans jamais
  // affirmer plus que lui. Elle n'en dérive rien : les limites sont dérivées par le runner, pas ici.
  var ROUTE_SANTE = "/api/v1/sante";
  var ROUTE_EVALS = "/api/v1/evals/latest";

  // Les **noms d'alerte** que `/api/v1/sante` publie, traduits. Un nom inconnu s'affiche tel quel :
  // taire une alerte qu'on ne sait pas nommer serait pire que la montrer brute.
  var ALERTES = {
    sans_gate: "aucune question-témoin ne valide ce document",
    gate_perime: "le gate a été obtenu avec un autre code, d'autres prompts ou d'autres modèles",
    source_absente: "le fichier source n'est pas présent à côté des artefacts",
    cle_fournisseur_absente:
      "aucune clé fournisseur n'est configurée : le service ne peut répondre à aucune question",
    rapport_illisible: "le rapport d'ingestion est présent mais illisible",
    rapport_etranger: "le rapport d'ingestion décrit un autre document",
    quarantaine: "document écarté au chargement",
    // I2 de la revue Codex 2.1 : la projection des titres rendue à *comprendre* a perdu des
    // catégories entières faute de place — le modèle classerait hors périmètre des sujets traités.
    perimetre_tronque:
      "la liste des rubriques annoncée au modèle a été tronquée : des sujets traités par ce " +
      "document seraient jugés hors périmètre",
    ungated_refuse_en_production:
      "ALLOW_UNGATED a été posé en production : la dérogation a été refusée",
    // AD-5 (story 2.1) : les deux verrous du dictionnaire des variantes. Deux noms parce que deux
    // causes et deux correctifs — signer le fichier, ou le régénérer sur le corpus servi.
    dictionnaire_non_valide:
      "le dictionnaire des variantes n'a été validé par personne : le refus « zéro hit » est " +
      "désactivé",
    dictionnaire_corpus_perime:
      "le dictionnaire des variantes décrit un autre corpus que celui qui est servi"
  };

  // Les **raisons de quarantaine** ne sont pas des noms d'alerte : `corpus/loader.py` les calcule et
  // `api/etat._alertes` les publie sous `alerte: "quarantaine"`, la raison dans `detail`. Les mettre
  // dans `ALERTES` en faisait du code mort — la page n'aurait jamais traduit un seul d'entre eux.
  // Ils sont donc cherchés là où ils arrivent : en préfixe du détail.
  var RAISONS = [
    ["gate_echoue", "les questions-témoins de ce document ont échoué"],
    ["bloquant_statique", "un contrôle bloquant du rapport d'ingestion pèse sur ce document"],
    ["sans_gate", "aucune question-témoin ne valide ce document"]
  ];

  function raisonTraduite(detail) {
    var d = String(detail || "");
    for (var i = 0; i < RAISONS.length; i++) {
      if (d.indexOf(RAISONS[i][0]) === 0) return RAISONS[i][1];
    }
    return "";
  }

  // ---------- nœuds : la description de ce qu'il faut peindre ----------

  function noeud(tag, cls, texte, enfants) {
    var n = { tag: tag };
    if (cls) n.cls = cls;
    if (texte !== undefined && texte !== null) n.texte = String(texte);
    if (enfants && enfants.length) n.enfants = enfants;
    return n;
  }

  function tableau(v) { return Array.isArray(v) ? v : []; }

  // **Le** formatage des nombres publiés, identique à `server/evals/publication.py::nombre`
  // (`DECIMALES = 4`). Sans lui, les surfaces divergeaient dès qu'une valeur ne tombait pas juste :
  // le Markdown écrivait `1.0000` là où la page écrivait `1`, et `0.0550 €` là où elle écrivait
  // `0.055 €`. L'AC compare les quatre surfaces « à l'octet des chiffres près » : il n'y a donc
  // qu'une règle, et elle est écrite deux fois parce que les deux langages ne partagent rien —
  // `tests/test_publication_evals.py` les confronte sur des valeurs qui ne tombent pas juste.
  var DECIMALES = 4;
  function nombre4(v) { return Number(v).toFixed(DECIMALES); }

  // ---------- lecture stricte du 200 ----------
  //
  // Patron de `tools/sinistre/sinistre.js` (revue Codex 1.9, tours 2 et 3) : **tout ce que la page
  // affiche est descendu jusqu'à la feuille**, et une **clé absente** n'est pas « le champ vaut
  // null ». `routes/sante.py` publie avec le défaut de FastAPI (`response_model_exclude_none=False`)
  // : pydantic sérialise toujours la clé, `null` compris. Un corps sans `gate_profile` n'est donc
  // pas un serveur qui n'a pas de gate, c'est un corps qu'aucune route n'a pu écrire — et le peindre
  // dirait sur l'état du système quelque chose que le serveur n'a pas dit.

  function estChaine(v) { return typeof v === "string"; }

  // **La** règle du couple (profil, compte), écrite une fois et partagée mot pour mot avec
  // `web/app/chat.js` — `tests/js/sante_corpus.mjs` rejoue les mêmes corps dans les deux lecteurs et
  // exige le même verdict, corps par corps :
  //
  //   - `gate_profile` : chaîne **non vide**, ou `null`. « niveau de validation :  — 2 cas » ne dit
  //     rien, et `routes/sante.py` n'écrit jamais une chaîne vide (`EtatApp.gate_profile` rend le
  //     profil d'un gate ou `None`).
  //   - `gate_cases` : entier **≥ 1**, ou `null`. Le plancher est 1 et non 0 : `evals/run.py` refuse
  //     de tourner sur zéro cas (« aucun cas au profil … »), donc aucun gate ne peut porter
  //     `cases: 0` et « vertical — 0 cas relu à la main » est une phrase que rien ne peut produire.
  //   - `gate_countersigned` : booléen, ou `null`. C'est lui, et non le nom du profil, qui décide
  //     si la page écrit « relus à la main » (revue Codex 1.10 tour 2). Un profil sans lui laisserait
  //     la page choisir entre deux phrases dont l'une affirme une relecture humaine.
  //   - les trois nuls, ou les trois non nuls : `EtatApp.gate_cases` et `EtatApp.gate_countersigned`
  //     rendent `null` dès que `gate_profile` l'est. Un corps qui les dissocie n'a pas été écrit par
  //     cette route.
  //
  // Tout ce que cette règle refuse est une **sonde illisible** (état 3), jamais un niveau à moitié
  // peint : c'est la différence entre « le serveur n'a pas répondu » et une phrase qu'il n'a pas dite.
  function profilLisible(p) { return p === null || (estChaine(p) && p.length > 0); }
  function compteLisible(n) {
    return n === null || (typeof n === "number" && isFinite(n) && Math.floor(n) === n && n >= 1);
  }
  function contresigneLisible(c) { return c === null || typeof c === "boolean"; }
  function coupleLisible(p, n, c) {
    return profilLisible(p) && compteLisible(n) && contresigneLisible(c) &&
      ((p === null) === (n === null)) && ((p === null) === (c === null));
  }

  // `dictionary` de `/api/v1/sante` (AD-5, story 2.1) : **trois booléens**, descendus jusqu'à la
  // feuille comme le reste de ce lecteur. Deux faits publiés par le serveur — `validated` (une main
  // a signé) et `corpus_ok` (les empreintes décrivent le corpus servi) — et la **règle** qu'ils
  // décident, `refus_zero_hit_actif`. La page ne refait pas la conjonction : `schemas.py` dit que la
  // règle n'a qu'une autorité, le serveur, et un lecteur qui la recopierait afficherait un jour un
  // refus armé qui ne l'est pas.
  //
  // Un `dictionary` absent, non objet, ou dont l'un des trois champs n'est pas un booléen est une
  // **sonde illisible** (état 3), au même titre qu'un `gate_profile` absent : `routes/sante.py`
  // sérialise toujours l'objet complet (`EtatDictionnaire` a trois champs, tous avec un défaut), si
  // bien qu'un corps amputé n'a été écrit par aucune route — et peindre « le refus est désactivé »
  // à partir d'une clé manquante dirait sur le système quelque chose que le serveur n'a pas dit.
  function lireDictionnaire(d) {
    if (!d || typeof d !== "object" || Array.isArray(d)) return null;
    if (typeof d.validated !== "boolean") return null;
    if (typeof d.corpus_ok !== "boolean") return null;
    if (typeof d.refus_zero_hit_actif !== "boolean") return null;
    return { validated: d.validated, corpus_ok: d.corpus_ok,
             refus_zero_hit_actif: d.refus_zero_hit_actif };
  }

  function lireAlerte(a) {
    if (!a || typeof a !== "object" || Array.isArray(a)) return null;
    if (!estChaine(a.doc_id) || !estChaine(a.alerte) || !estChaine(a.detail)) return null;
    return { doc_id: a.doc_id, alerte: a.alerte, detail: a.detail };
  }

  /** Le corps de `/api/v1/sante` réduit à ce que cette page affiche, ou `null` s'il est illisible. */
  function lireSante(o) {
    if (!o || typeof o !== "object" || Array.isArray(o)) return null;
    if (typeof o.ok !== "boolean" || !estChaine(o.version)) return null;
    if (!Array.isArray(o.documents_servis) || !o.documents_servis.every(estChaine)) return null;
    if (!coupleLisible(o.gate_profile, o.gate_cases, o.gate_countersigned)) return null;
    var dictionnaire = lireDictionnaire(o.dictionary);
    if (dictionnaire === null) return null;
    if (!Array.isArray(o.alerts)) return null;
    var alertes = [];
    for (var i = 0; i < o.alerts.length; i++) {
      var a = lireAlerte(o.alerts[i]);
      if (a === null) return null;
      alertes.push(a);
    }
    return {
      ok: o.ok, version: o.version, documents_servis: o.documents_servis,
      gate_profile: o.gate_profile, gate_cases: o.gate_cases,
      gate_countersigned: o.gate_countersigned, dictionary: dictionnaire, alerts: alertes
    };
  }

  // ---------- lecture stricte de `/api/v1/evals/latest` (FR41) ----------
  //
  // Même patron que `lireSante` : **tout ce que la page affiche est descendu jusqu'à la feuille**, et
  // une clé absente n'est pas « le champ vaut zéro ». `routes/evals.py` sérialise `EtatPublication`
  // complet ; un corps amputé n'a été écrit par aucune route, et peindre un recall à partir d'une clé
  // manquante dirait sur la mesure quelque chose que le serveur n'a pas dit.
  //
  // Trois issues, et trois seulement :
  //   - `{publie: false, …}`   → aucun run publié. C'est un **état normal**, pas une panne.
  //   - `{publie: true, publication: {…}}` → les chiffres du dernier run, tels quels.
  //   - `null` (ce lecteur)    → le corps n'est pas celui que cette page sait lire : la sonde a
  //                              échoué, et l'échec d'une sonde ne se peint jamais en « aucun run ».

  function estNombreFini(v) { return typeof v === "number" && isFinite(v); }
  function estEntierPositif(v) { return estNombreFini(v) && Math.floor(v) === v && v >= 0; }

  function lireComptes(o) {
    if (!o || typeof o !== "object" || Array.isArray(o)) return null;
    var lu = {};
    var cles = Object.keys(o);
    for (var i = 0; i < cles.length; i++) {
      if (!estEntierPositif(o[cles[i]])) return null;
      lu[cles[i]] = o[cles[i]];
    }
    return lu;
  }

  function lireListeDeChaines(v) {
    if (!Array.isArray(v)) return null;
    for (var i = 0; i < v.length; i++) if (!estChaine(v[i])) return null;
    return v.slice();
  }

  function lireReserves(r) {
    if (!r || typeof r !== "object" || Array.isArray(r)) return null;
    if (typeof r.countersigned !== "boolean") return null;
    if (typeof r.validated_by_expert !== "boolean") return null;
    if (typeof r.dictionary_validated !== "boolean") return null;
    return { countersigned: r.countersigned, validated_by_expert: r.validated_by_expert,
             dictionary_validated: r.dictionary_validated };
  }

  function lirePublication(p) {
    if (!p || typeof p !== "object" || Array.isArray(p)) return null;
    if (!estChaine(p.profile) || !p.profile.length) return null;
    if (!estChaine(p.run_digest) || !estChaine(p.cases_hash)) return null;
    if (!(p.candidate_revision === null || estChaine(p.candidate_revision))) return null;
    if (typeof p.evals_ok !== "boolean") return null;
    if (!estNombreFini(p.recall) || !estNombreFini(p.ne_tranche_pas_rate)) return null;
    var labels = lireComptes(p.labels);
    var variantes = lireComptes(p.variantes);
    if (labels === null || variantes === null) return null;
    var s = p.stabilite;
    if (!s || typeof s !== "object" || Array.isArray(s)) return null;
    if (!estEntierPositif(s.n) || !estEntierPositif(s.cas_stables)
        || !estEntierPositif(s.cas_comptabilises)) return null;
    var c = p.cout;
    if (!c || typeof c !== "object" || Array.isArray(c)) return null;
    if (!estNombreFini(c.froid_eur) || !estNombreFini(c.moyen_eur) || !estNombreFini(c.p95_eur)) {
      return null;
    }
    var l = p.latence;
    if (!l || typeof l !== "object" || Array.isArray(l)) return null;
    if (!estEntierPositif(l.p50_ms) || !estEntierPositif(l.p95_ms)) return null;
    var reserves = lireReserves(p.reserves);
    var limites = lireListeDeChaines(p.limites);
    if (reserves === null || limites === null) return null;
    return {
      profile: p.profile, candidate_revision: p.candidate_revision, run_digest: p.run_digest,
      cases_hash: p.cases_hash, evals_ok: p.evals_ok, labels: labels, variantes: variantes,
      recall: p.recall, ne_tranche_pas_rate: p.ne_tranche_pas_rate,
      stabilite: { n: s.n, cas_stables: s.cas_stables, cas_comptabilises: s.cas_comptabilises },
      cout: { froid_eur: c.froid_eur, moyen_eur: c.moyen_eur, p95_eur: c.p95_eur },
      latence: { p50_ms: l.p50_ms, p95_ms: l.p95_ms },
      reserves: reserves, limites: limites
    };
  }

  /** Le corps de `/api/v1/evals/latest` réduit à ce que cette page affiche, ou `null`. */
  function lireEvals(o) {
    if (!o || typeof o !== "object" || Array.isArray(o)) return null;
    if (typeof o.publie !== "boolean") return null;
    if (!o.publie) {
      if (!(o.raison === null || estChaine(o.raison))) return null;
      return { publie: false, raison: o.raison };
    }
    var publication = lirePublication(o.publication);
    if (publication === null) return null;
    return { publie: true, raison: null, publication: publication };
  }

  // ---------- composition pure ----------

  /** La phrase du niveau de validation. `sante` null ⇒ la sonde n'a pas répondu (état 3). */
  function libelleValidation(sante) {
    if (!sante) {
      return {
        etat: "inconnu",
        texte: "niveau de validation : inconnu (le serveur n'a pas répondu)"
      };
    }
    if (sante.gate_profile === null) {
      return {
        etat: "sans_gate",
        texte: "niveau de validation : aucun gate — aucun document n'est validé par des " +
               "questions-témoins"
      };
    }
    var n = sante.gate_cases;
    // « relus à la main » n'est vrai que du profil `vertical` : AD-14 le définit comme « un cas
    // guide et un cas sinistre **relus à la main** ». `full` (story 4.1) est la politique complète,
    // qui ne promet aucune relecture humaine — l'écrire quand même ferait affirmer à la page une
    // relecture qui n'a pas eu lieu, exactement la classe d'invention que D8 interdit. Le profil
    // vient du serveur ; la qualification, elle, n'est écrite que là où elle est vraie.
    //
    // Et le profil ne suffit pas (revue Codex 1.10 tour 2, B2) : `vertical` dit quelle politique de
    // mesure a tourné, `gate_countersigned` dit si la relecture qu'AD-14 met dans sa définition a
    // été contresignée par la personne à qui `epics.md` l'attribue. Tant qu'elle est due, la
    // relecture est celle de la boucle autonome, et la page le dit — c'est la même règle que pour
    // `full` : la qualification n'est écrite que là où elle est vraie, et le serveur seul l'établit.
    var relus = "";
    if (sante.gate_profile === "vertical") {
      relus = sante.gate_countersigned
        ? " relu" + (n > 1 ? "s" : "") + " à la main"
        : " relu" + (n > 1 ? "s" : "") + " par la boucle, contresignature humaine en attente";
    }
    return {
      etat: "gate",
      contresigne: sante.gate_countersigned,
      texte: "niveau de validation : " + sante.gate_profile + " — " + n + " cas" + relus
    };
  }

  /** Le serveur signale-t-il que le gate ne correspond plus à l'image qui tourne ? */
  function perime(sante) {
    return tableau(sante && sante.alerts).some(function (a) { return a.alerte === "gate_perime"; });
  }

  /** Le serveur signale-t-il que le dictionnaire chargé décrit un **autre** corpus ? */
  function dictionnairePerime(sante) {
    return tableau(sante && sante.alerts).some(function (a) {
      return a.alerte === "dictionnaire_corpus_perime";
    });
  }

  /** La phrase du dictionnaire des variantes — quatre formulations, aucune règle recalculée.
   *
   * AD-5 / AD-16 : un dictionnaire inutilisable est **dit**. Ce que la ligne annonce est le sort du
   * refus « zéro hit » — la seule chose que la validation humaine arme —, et il est lu tel quel sur
   * `refus_zero_hit_actif`, jamais recomposé à partir des deux faits.
   *
   * **Quatre** états, parce que le serveur en établit quatre et qu'ils n'ont pas les mêmes
   * conséquences pour celui qui pose une question (revue coordonnée 2.1) :
   *
   *   1. `refus_zero_hit_actif` — signé et conforme : le refus est armé ;
   *   2. l'alerte `dictionnaire_corpus_perime` — le fichier se lit mais décrit un autre corpus :
   *      ni ses variantes ni le refus ;
   *   3. `corpus_ok` sans cette alerte — chargé, conforme, **pas signé** : ses variantes élargissent
   *      réellement la recherche, seul le refus dort ;
   *   4. ni l'un ni l'autre — rien n'est chargé : la recherche est exactement celle d'avant.
   *
   * Les trois et quatre disaient la même phrase (« aucune validation humaine ») alors que la
   * différence est matérielle : dans un cas une question en anglais ouvre la bonne fiche, dans
   * l'autre non. `corpus_ok` la porte, le serveur la publie, la page n'a rien à en déduire.
   *
   * Ce qui distingue « périmé » d'« absent » reste l'**alerte du serveur**, jamais un calcul de la
   * page (même patron que `perime()` pour le gate) : `corpus_ok` vaut `false` dans les deux cas, ils
   * n'ont pas le même correctif — régénérer, ou ingérer pour la première fois —, et le serveur seul
   * sait les séparer. Il le dit en publiant `dictionnaire_corpus_perime` exactement quand le fichier
   * se lit sans décrire le corpus servi (`api/etat._alertes_dictionnaire`). Écrire « périmé » sur la
   * seule foi de `corpus_ok` ferait annoncer un fichier périmé là où il n'y a aucun fichier.
   */
  function libelleDictionnaire(sante) {
    var d = sante && sante.dictionary;
    if (!d) return null;
    if (d.refus_zero_hit_actif) {
      return {
        etat: "arme",
        texte: "dictionnaire des variantes : validé, et ses empreintes décrivent le corpus servi " +
               "— le refus « zéro hit » est armé : une question dont aucun terme ni aucune variante " +
               "n'a de passage est refusée avec sa preuve, sans appel de raisonnement."
      };
    }
    if (dictionnairePerime(sante)) {
      return {
        etat: "corpus_perime",
        texte: "dictionnaire des variantes : il décrit un autre corpus que celui qui est servi — " +
               "le refus « zéro hit » est désactivé, et ses variantes ne sont pas employées."
      };
    }
    if (d.corpus_ok) {
      return {
        etat: "non_signe",
        texte: "dictionnaire des variantes : chargé, et ses empreintes décrivent le corpus servi, " +
               "mais personne ne l'a signé — ses variantes élargissent bien la recherche ; seul le " +
               "refus « zéro hit » est désactivé, faute de validation humaine."
      };
    }
    return {
      etat: "absent",
      texte: "dictionnaire des variantes : aucun dictionnaire n'est chargé — le refus « zéro hit » " +
             "est désactivé, et la recherche se fait sur les seuls termes de la question, sans " +
             "aucune variante."
    };
  }

  /** Les alertes du serveur, en français, sans jamais rien en déduire de plus. */
  function vueAlertes(alerts) {
    var items = tableau(alerts).map(function (a) {
      var connu = Object.prototype.hasOwnProperty.call(ALERTES, a.alerte);
      var phrase = (a.doc_id === "*" ? "" : a.doc_id + " : ") +
        (connu ? ALERTES[a.alerte] : a.alerte) +
        (connu ? " (" + a.alerte + ")" : "");
      var traduite = raisonTraduite(a.detail);
      if (traduite) phrase += " : " + traduite;
      if (a.detail) phrase += " — " + a.detail;
      return noeud("li", null, phrase);
    });
    if (!items.length) return null;
    return noeud("div", "alertes", null, [
      noeud("h3", null, "Alertes du serveur"),
      noeud("ul", null, null, items)
    ]);
  }

  /** L'état du système, tel que la sonde l'a dit. */
  function vueEtat(sante) {
    // Sans corps lu, il n'y a pas d'état à peindre : c'est l'état 3, et il a sa vue. La garde n'est
    // pas de la coquetterie — sans elle, un appelant qui passerait `null` lèverait un `TypeError`
    // sur `sante.documents_servis` **après** la sonde, et laisserait le bloc vide sans un mot
    // (la même leçon que `peindre()` en 1.9).
    if (!sante) return vueSondeEchouee(null);
    var validation = libelleValidation(sante);
    var enfants = [noeud("p", "validation", validation.texte)];
    if (validation.etat === "gate") {
      // La phrase disait « il porte les empreintes du corpus, du code et des prompts qui l'ont
      // obtenu » — vrai en général, **faux** précisément quand le serveur lève `gate_perime`, qui
      // veut dire que ces empreintes ne sont plus celles de l'image servie. AD-7 garde le document
      // servi dans ce cas ; ce qu'il ne permet pas, c'est de le dire à l'envers. La page n'affirme
      // donc plus que ce que la sonde établit, et le péremption a sa propre ligne.
      if (perime(sante)) {
        enfants.push(noeud("p", "detail",
          "Réserve : le serveur signale que ce gate a été obtenu avec un autre code, d'autres " +
          "prompts ou d'autres modèles que ceux qui tournent ici. Le niveau ci-dessus décrit ce " +
          "qui a été mesuré alors, pas ce qui tourne maintenant."));
      } else {
        enfants.push(noeud("p", "detail",
          "Ce gate a été écrit par un run réel des questions-témoins sur le corpus servi, et le " +
          "serveur ne signale aucun écart entre les empreintes qu'il porte et l'image qui tourne. " +
          "Les verdicts ne sont validés par aucun expert assurance."));
      }
    } else if (!sante.documents_servis.length) {
      // `gate_profile` est aussi `null` quand **rien** n'est servi : le dire « aucun gate » sans
      // préciser cela laisserait croire qu'un corpus attend d'être validé.
      enfants.push(noeud("p", "detail",
        "Aucun document n'est servi : le serveur ne peut répondre à aucune question."));
    } else {
      enfants.push(noeud("p", "detail",
        "Au moins un document servi n'est validé par aucune question-témoin, ou deux documents ne " +
        "portent pas le même profil. Les alertes ci-dessous disent lequel et pourquoi."));
    }
    enfants.push(noeud("p", "detail",
      "documents servis : " + (sante.documents_servis.length
        ? sante.documents_servis.join(", ") : "aucun")));
    enfants.push(noeud("p", "detail", "version servie : " + sante.version));
    // AD-5 : le second verrou du système, à côté du gate. Il ne conditionne pas le niveau de
    // validation — un corpus gaté répond parfaitement sans dictionnaire —, il dit si le **refus**
    // est armé, c'est-à-dire si une question hors du guide ressort avec sa preuve d'absence ou
    // continue vers la recherche. Le taire laisserait lire « vertical » comme si tout était armé.
    var dictionnaire = libelleDictionnaire(sante);
    if (dictionnaire) enfants.push(noeud("p", "detail", dictionnaire.texte));
    if (sante.ok !== true) {
      // `ok` est ce que le front du guide lit pour décider s'il peut poser une question : le taire
      // ici afficherait un état normal alors que le guide n'est pas servi.
      enfants.push(noeud("p", "detail",
        "Le serveur répond, mais il annonce que l'assistant du guide n'est pas disponible : une " +
        "question posée dans le guide n'obtiendrait pas de réponse."));
    }
    var alertes = vueAlertes(sante.alerts);
    if (alertes) enfants.push(alertes);
    return noeud("div", "carte etat-" + validation.etat, null, enfants);
  }

  var RAISONS_EVALS = {
    absent: "aucun run n'a encore été publié dans cette image",
    illisible: "le fichier de résultats est présent mais ne se lit pas (octets ou JSON invalides)",
    hors_schema: "le fichier de résultats ne correspond pas au format que ce serveur sait lire"
  };

  /** Les lignes chiffrées du dernier run publié — **lues**, jamais recalculées.
   *
   * FR42 : `/` reprend le **même artefact** que `docs/evals/latest.md`, que le résumé de CI et que
   * `GET /api/v1/evals/latest`. Cette fonction ne fait donc aucune arithmétique : pas de moyenne
   * recalculée, pas de taux redérivé, pas de limite rédigée ici. Les limites viennent du runner, qui
   * les dérive du run ; une phrase composée par la page serait une affirmation que le serveur n'a
   * pas faite, et c'est exactement ce que D8 et AD-16 interdisent.
   *
   * Trois états, comme pour la sonde de santé :
   *   1. `evals` null            → la sonde des résultats n'a pas répondu : dit, et rien d'autre ;
   *   2. `publie: false`         → **l'absence est rendue comme une absence**, avec sa raison ;
   *   3. `publie: true`          → les chiffres, le verdict, les réserves et les limites.
   */
  function vueEvals(evals, motif) {
    if (!evals) {
      return noeud("div", "carte evals-inconnu", null, [
        noeud("p", "inconnu", "résultats des questions-témoins : inconnus (le serveur n'a pas " +
                              "répondu)"),
        noeud("p", "detail", motifTexte(motif)),
        noeud("p", "detail", "Aucun chiffre n'est affiché à la place : une mesure qui n'a pas été " +
                             "lue ne s'invente pas.")
      ]);
    }
    if (!evals.publie) {
      var raison = evals.raison && Object.prototype.hasOwnProperty.call(RAISONS_EVALS, evals.raison)
        ? RAISONS_EVALS[evals.raison]
        : (evals.raison || "aucun run publié");
      return noeud("div", "carte evals-absent", null, [
        noeud("p", "validation", "résultats des questions-témoins : aucun run publié"),
        noeud("p", "detail", "Le serveur le dit ainsi : " + raison + "."),
        noeud("p", "detail", "Aucun chiffre n'est affiché à la place.")
      ]);
    }
    var p = evals.publication;
    var enfants = [
      noeud("p", "validation",
        "résultats des questions-témoins : gate " + (p.evals_ok ? "vert" : "rouge") +
        " — profil " + p.profile),
      noeud("p", "detail",
        "Publié tel quel, rouge compris. Publier ne promeut rien : ce qui est servi est décidé par " +
        "le gate, pas par cette page.")
    ];
    enfants.push(noeud("p", "detail", "rappel : " + nombre4(p.recall)));
    enfants.push(noeud("p", "detail",
      "stabilité : " + p.stabilite.cas_stables + "/" + p.stabilite.cas_comptabilises +
      " cas stables sur N=" + p.stabilite.n + " répétitions"));
    enfants.push(noeud("p", "detail",
      "coût : " + nombre4(p.cout.froid_eur) + " € froid, " + nombre4(p.cout.moyen_eur) +
      " € en moyenne par exécution, " + nombre4(p.cout.p95_eur) + " € au p95"));
    enfants.push(noeud("p", "detail",
      "latence : " + p.latence.p50_ms + " ms p50, " + p.latence.p95_ms + " ms p95"));
    enfants.push(noeud("p", "detail",
      "taux de ne_tranche_pas : " + nombre4(p.ne_tranche_pas_rate)));
    enfants.push(noeud("p", "detail", "labels : " + comptesEnTexte(p.labels)));
    enfants.push(noeud("p", "detail", "variantes : " + comptesEnTexte(p.variantes)));
    enfants.push(noeud("p", "detail", "cases_hash : " + p.cases_hash));
    enfants.push(noeud("p", "detail", "run_digest : " + p.run_digest));
    enfants.push(noeud("p", "detail",
      "révision candidate : " + (p.candidate_revision || "non renseignée")));
    // Les trois réserves de l'AC : lues sur le serveur, jamais recomposées, et jamais tues même
    // quand elles sont toutes fausses — c'est précisément là qu'elles comptent.
    enfants.push(noeud("p", "detail",
      "réserves — contresignature humaine : " + (p.reserves.countersigned ? "oui" : "non") +
      " ; validation par un expert assurance : " +
      (p.reserves.validated_by_expert ? "oui" : "non") +
      " ; dictionnaire des variantes validé : " +
      (p.reserves.dictionary_validated ? "oui" : "non")));
    if (p.limites.length) {
      enfants.push(noeud("div", "limites", null, [
        noeud("h3", null, "Limites de ce run"),
        noeud("ul", null, null, p.limites.map(function (limite) {
          return noeud("li", null, limite);
        }))
      ]));
    }
    return noeud("div", "carte evals-" + (p.evals_ok ? "vert" : "rouge"), null, enfants);
  }

  /** `{bonne_reponse: 2, parsing: 1}` → « bonne_reponse ×2, parsing ×1 » — tri stable, sans total. */
  function comptesEnTexte(comptes) {
    var cles = Object.keys(comptes).sort();
    if (!cles.length) return "aucun";
    return cles.map(function (cle) { return cle + " ×" + comptes[cle]; }).join(", ");
  }

  /** La sonde n'a pas répondu : on le dit, et on n'affiche **aucun** niveau (AD-16, D8). */
  function vueSondeEchouee(motif) {
    var validation = libelleValidation(null);
    return noeud("div", "carte etat-inconnu", null, [
      noeud("p", "inconnu", validation.texte),
      noeud("p", "detail", motifTexte(motif)),
      noeud("p", "detail",
        "Aucun niveau n'est affiché à la place : un profil de validation qui n'a pas été lu ne " +
        "s'invente pas.")
    ]);
  }

  function motifTexte(motif) {
    if (motif === "hors_ligne") {
      return "Cette page est ouverte depuis un fichier local : il n'y a aucun serveur à interroger.";
    }
    if (motif === "reponse_illisible") {
      return "Le serveur a répondu, mais sa réponse n'est pas celle que cette page sait lire.";
    }
    if (motif === "timeout_client") {
      return "Le serveur n'a pas répondu dans le délai imparti.";
    }
    if (typeof motif === "string" && motif.indexOf("http_") === 0) {
      // Le serveur a bien répondu — mais pas un état de santé. Le dire autrement (« pas de gate »)
      // serait une affirmation sur le système à partir d'une réponse qui n'en parle pas (AD-16).
      return "Le serveur a répondu " + motif.slice(5) + " : ce n'est pas un état de santé.";
    }
    return "Le serveur n'a pas répondu.";
  }

  // ---------- le matérialiseur : il peint, il ne décide pas ----------
  //
  // AD-15 : tout ce qui vient du serveur est posé par `textContent`. `innerHTML` n'est employé que
  // pour **vider** un conteneur (chaîne vide) — le DOM minimal des tests lève sur toute autre pose.
  function materialiser(vue) {
    var e = document.createElement(vue.tag);
    if (vue.cls) e.className = vue.cls;
    if (vue.texte !== undefined) e.textContent = vue.texte;
    (vue.enfants || []).forEach(function (enfant) { e.appendChild(materialiser(enfant)); });
    return e;
  }

  function peindre(vue, cible) {
    var hote = cible || document.getElementById("etat");
    if (!hote) return null;
    hote.innerHTML = "";
    var e = materialiser(vue);
    hote.appendChild(e);
    return e;
  }

  function peindreDans(id, vue) {
    var hote = document.getElementById(id);
    return hote ? peindre(vue, hote) : null;
  }

  // ---------- réseau ----------

  function enLigne() { return !!API_BASE; }

  /** `GET /api/v1/sante`. Rend le corps lu, ou rejette avec un motif — jamais une valeur inventée. */
  function sonder() {
    return sonderRoute(ROUTE_SANTE, lireSante);
  }

  /** `GET /api/v1/evals/latest`, exactement le même contrat de sonde que `/sante`. */
  function sonderEvals() {
    return sonderRoute(ROUTE_EVALS, lireEvals);
  }

  /** Une sonde : une route, un lecteur strict, un budget total. Rien d'autre n'est partagé. */
  function sonderRoute(route, lire) {
    if (!enLigne()) return Promise.reject("hors_ligne");
    if (typeof fetch !== "function") return Promise.reject("reseau");
    var opts = { method: "GET" };
    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    if (ctrl) opts.signal = ctrl.signal;
    var minuteur = ctrl
      ? setTimeout(function () { ctrl.abort(); }, Math.round(SONDE_BUDGET_S * 1000))
      : null;
    function finir() { if (minuteur !== null) clearTimeout(minuteur); }
    var envoi;
    try {
      envoi = fetch(API_BASE + route, opts);
    } catch (e) {
      // Un `fetch` qui lève **de façon synchrone** (URL rejetée, navigateur ancien) sortait de
      // `sonder()` par une exception, pas par un rejet : `demarrer()` ne la voyait pas, et la page
      // restait muette — ni niveau, ni message d'échec.
      finir();
      return Promise.reject("reseau");
    }
    // `finir()` n'est appelé qu'au **règlement** de la promesse, pas à la réception des en-têtes :
    // `r.json()` attend le corps, et l'annuler plus tôt laissait un corps qui ne se termine jamais
    // bloquer la page indéfiniment — sur le seul état qu'elle ne sait pas dire (ni niveau, ni échec).
    return envoi.then(function (r) {
      // Un non-200 n'est jamais un état du système : ni « pas de gate » sur `/sante`, ni « aucun
      // run publié » sur `/evals/latest`. C'est une sonde qui a échoué (AD-16), et le dire est le
      // seul état honnête — le serveur publie ses absences en 200, avec leur raison.
      if (!r.ok) throw "http_" + r.status;
      return r.json().then(function (j) {
        var lu = lire(j);
        if (lu === null) throw "reponse_illisible";
        return lu;
      }, function () { throw "reponse_illisible"; });
    }, function () {
      throw (ctrl && ctrl.signal.aborted) ? "timeout_client" : "reseau";
    }).then(function (lu) {
      finir();
      return lu;
    }, function (motif) {
      finir();
      throw (ctrl && ctrl.signal.aborted && motif === "reponse_illisible")
        ? "timeout_client" : motif;
    });
  }

  // ---------- démarrage : le seul endroit qui touche la page ----------

  function demarrer() {
    // Deux sondes **indépendantes**, et c'est délibéré : l'échec de l'une ne doit jamais effacer ce
    // que l'autre a établi. Un serveur qui répond `/sante` mais dont les résultats d'évals ne sont
    // pas lisibles reste un serveur dont on connaît le niveau de validation — et l'inverse est vrai
    // aussi. Les fusionner en une seule promesse ferait disparaître les deux au premier échec.
    var etat = sonder().then(function (sante) {
      peindre(vueEtat(sante));
      return sante;
    }, function (motif) {
      peindre(vueSondeEchouee(motif));
      return null;
    });
    var evals = sonderEvals().then(function (lu) {
      peindreDans("evals", vueEvals(lu));
      return lu;
    }, function (motif) {
      // Le **motif réel**, comme la sonde de `/sante` le propage : « ouverte en fichier local »,
      // « le serveur a répondu 503 » et « la réponse n'est pas celle que cette page sait lire » ne
      // sont pas la même panne, et n'ont pas le même correctif. Les rabattre sur un message unique
      // faisait disparaître l'information au moment précis où elle sert.
      peindreDans("evals", vueEvals(null, motif));
      return null;
    });
    return Promise.all([etat, evals]).then(function (deux) {
      return deux[0];
    });
  }

  window.ACCUEIL = {
    // Composition pure : testable sans navigateur (`tests/js/accueil_cases.mjs`).
    lireSante: lireSante,
    coupleLisible: coupleLisible,
    lireDictionnaire: lireDictionnaire,
    libelleValidation: libelleValidation,
    libelleDictionnaire: libelleDictionnaire,
    perime: perime,
    dictionnairePerime: dictionnairePerime,
    vueEtat: vueEtat,
    vueAlertes: vueAlertes,
    vueSondeEchouee: vueSondeEchouee,
    motifTexte: motifTexte,
    // FR41/FR42 : les résultats publiés, lus et composés purement.
    lireEvals: lireEvals,
    vueEvals: vueEvals,
    comptesEnTexte: comptesEnTexte,
    nombre4: nombre4,
    RAISONS_EVALS: RAISONS_EVALS,
    routes: function () { return { sante: ROUTE_SANTE, evals: ROUTE_EVALS }; },
    // Réseau et peinture.
    sonder: sonder,
    sonderEvals: sonderEvals,
    materialiser: materialiser,
    peindre: peindre,
    demarrer: demarrer,
    apiBase: function () { return API_BASE; },
    setApiBase: function (u) { API_BASE = u; },
    bornes: function () { return { sonde_budget_s: SONDE_BUDGET_S }; },
    ALERTES: ALERTES,
    RAISONS: RAISONS,
    raisonTraduite: raisonTraduite
  };

  // Le harnais de test charge ce fichier sans page : il pose ce drapeau pour obtenir
  // `window.ACCUEIL` sans démarrage. Même mécanique que `__SINISTRE_SANS_DEMARRAGE`.
  if (!window.__ACCUEIL_SANS_DEMARRAGE) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", demarrer);
    } else {
      demarrer();
    }
  }
})();
