// Astuces de la communauté.
//
// Principe : chaque fiche sépare deux registres.
//   1. le corps de la fiche, qui ne contient que de l'information officielle,
//      vérifiable et sourcée ;
//   2. ce fichier, qui rassemble des retours d'expérience de personnes déjà
//      installées. Utiles, mais sans valeur officielle, et présentés comme tels.
//
// Ce fichier est autonome : il s'ajoute à la base, injecte son propre style et
// affiche le bloc sous la fiche ouverte, sans modifier les autres fichiers.
// Contenu générique et anonyme, aucune donnée personnelle.

(function () {
  "use strict";

  var ASTUCES = {
    arrivee: [
      "Prenez le rendez-vous à la commune avant même d'avoir les clés : en août et en septembre, les créneaux partent plusieurs semaines à l'avance et le délai de huit jours arrive vite.",
      "Photographiez le certificat de résidence dès sa remise. On vous en demandera une copie une dizaine de fois les premiers mois, et l'avoir sur son téléphone évite de repasser à la commune."
    ],
    matricule: [
      "Communiquez votre IBAN à la caisse dès la première demande de remboursement. Beaucoup l'oublient, découvrent que rien n'arrive, et perdent plusieurs semaines.",
      "Gardez les justificatifs papier des premiers mois : le temps que le circuit s'amorce, c'est le seul moyen de vérifier qu'aucune demande ne s'est perdue."
    ],
    luxtrust: [
      "Demandez LuxTrust au moment même de l'ouverture du compte bancaire, dans le même rendez-vous. Y revenir plus tard suppose un second passage en agence.",
      "Installez l'application mobile plutôt que de dépendre d'un lecteur physique : c'est plus rapide au quotidien, et cela évite de chercher un appareil au moment de signer une démarche."
    ],
    recherche_logement: [
      "Visitez aux heures où vous vivrez réellement dans le logement, en fin de journée par exemple. Un appartement calme à quatorze heures peut donner sur un axe passant à dix-huit heures.",
      "Demandez le montant des charges de l'année précédente, et pas seulement la provision mensuelle annoncée. L'écart au décompte annuel surprend souvent.",
      "Si votre employeur propose un accompagnement à la relocation, utilisez-le pour la mise en relation, mais gardez la décision : personne ne connaît vos contraintes de trajet mieux que vous."
    ],
    choisir_commune: [
      "Faites le trajet en conditions réelles avant de signer, un matin de semaine, école comprise. Les simulateurs d'itinéraire sous-estiment régulièrement les heures de pointe.",
      "Regardez la fréquence du bus le samedi et en soirée, pas seulement en semaine. C'est là que les écarts entre communes sont les plus violents.",
      "Une commune un peu plus loin mais sur une ligne directe bat souvent une commune proche nécessitant une correspondance."
    ],
    bail: [
      "Filmez l'état des lieux d'entrée en plus des photographies, en commentant à voix haute pièce par pièce. C'est plus rapide à faire, et bien plus convaincant deux ans plus tard.",
      "Relevez vous-même les compteurs le jour de la remise des clés, et envoyez les photos au bailleur le soir même.",
      "Un accord verbal du bailleur ne vaut rien au moment de l'état des lieux de sortie. Faites confirmer par courriel, même pour une étagère."
    ],
    assurance_habitation: [
      "Souscrivez dès la promesse de location, avec la date d'effet au jour de la remise des clés. Attendre le dernier moment expose à ne pas avoir l'attestation le jour de la signature.",
      "Faites l'inventaire de ce que vous possédez, avec photos, avant l'emménagement. Sans cet inventaire, l'indemnisation se négocie de mémoire."
    ],
    assurance_sante: [
      "Souscrivez la complémentaire à l'arrivée, avant d'en avoir besoin. Les délais de carence sur le dentaire et l'orthodontie se comptent en mois, et personne n'y pense avant le premier devis.",
      "Si vous continuez à consulter dans votre pays d'origine les premières années, vérifiez explicitement que le contrat couvre les soins reçus à l'étranger."
    ],
    achat: [
      "Demandez au promoteur, par écrit, si le prix affiché intègre déjà la part de TVA à 17 %. La réponse change parfois le budget de plusieurs dizaines de milliers d'euros.",
      "Mettez l'assurance solde restant dû en concurrence, séparément du prêt. C'est le poste où l'écart entre établissements est le plus large, et il se négocie mal une fois le crédit signé."
    ],
    emmenagement: [
      "Mesurez la cabine d'ascenseur et prenez-en une photo avec un mètre déroulé. Vous l'aurez sous la main au magasin, au moment de choisir entre deux hauteurs d'armoire.",
      "Réservez le monteur avant la livraison, pas après. En période de déménagements, les délais s'allongent et les cartons restent dans le couloir.",
      "Gardez quelques cartons et l'emballage d'origine des meubles fragiles : le prochain déménagement arrive plus vite qu'on ne le croit."
    ],
    rdv_technique: [
      "Envoyez les précisions d'accès par le canal qui laisse une trace, portail ou courriel, et gardez la capture d'écran avec l'horodatage. C'est ce qui fait la différence si vous devez contester.",
      "Prévenez qu'un appel masqué ne peut pas être rappelé, et demandez un numéro joignable. C'est la cause d'échec la plus fréquente, et la plus facile à éviter.",
      "Si quelqu'un d'autre attend sur place, donnez son numéro et vérifiez la sonnette la veille."
    ],
    reclamation: [
      "Après un appel au service client, envoyez un courriel récapitulant ce qui vous a été dit. Cela crée la trace écrite que l'appel ne laisse pas, et cela suffit souvent à faire bouger le dossier.",
      "Citez en priorité les documents de l'entreprise elle-même, compte rendu d'intervention ou accusé de lecture. Un argument tiré de leurs propres pièces se conteste mal.",
      "Annoncez calmement la suite, sans agressivité. La perspective d'une médiation gratuite règle beaucoup de dossiers avant qu'elle ne commence."
    ],
    telecom: [
      "Souscrivez avant même d'avoir emménagé si la date de bail est connue : le délai court à partir de la commande, pas de votre arrivée.",
      "Réclamez explicitement la solution 4G provisoire. Elle existe chez plusieurs opérateurs mais n'est presque jamais proposée spontanément.",
      "Évitez si possible de programmer l'installation entre juin et septembre, période où les délais s'allongent le plus."
    ],
    banque: [
      "Prenez rendez-vous avant l'arrivée si l'agence l'accepte : le compte conditionne la garantie locative, qui conditionne le logement.",
      "Demandez d'emblée si la garantie locative est incluse ou facturée, et sous quel délai elle est émise. Les pratiques diffèrent nettement d'une banque à l'autre."
    ],
    ecole: [
      "Contactez les établissements avant d'avoir une adresse définitive. Attendre le logement fait perdre une saison d'inscriptions.",
      "Demandez explicitement s'il reste des places dans la section linguistique visée, et pas seulement dans l'école. C'est la section qui sature.",
      "Récupérez le certificat de radiation et les bulletins avant de partir. Les obtenir à distance, une fois installé, prend des semaines."
    ],
    garde: [
      "Inscrivez-vous sur plusieurs listes d'attente en parallèle, sans attendre la date d'arrivée. Se désinscrire est facile, entrer sur une liste tardivement ne l'est pas.",
      "Vérifiez le conventionnement de la structure avant de signer : l'écart de coût avec une place non conventionnée se compte en centaines d'euros par mois."
    ],
    vehicule: [
      "Réclamez le relevé d'informations à votre assureur avant de résilier. Une fois le contrat clos, l'obtenir devient plus difficile, et sans lui le bonus ne se transfère pas.",
      "En achat entre particuliers, venez avec le vendeur lors de l'immatriculation : cela évite les allers-retours si une pièce manque."
    ],
    permis: [
      "L'enregistrement est gratuit et se fait par courrier : profitez-en pendant que vous rassemblez déjà des copies de documents pour d'autres démarches.",
      "Photographiez recto et verso de votre permis et rangez les images en lieu sûr. En cas de perte, cela accélère tout."
    ],
    transport: [
      "Testez la ligne un samedi avant de choisir une commune : la fréquence du week-end révèle mieux la desserte réelle que celle des heures de pointe.",
      "Les parkings relais gratuits en périphérie évitent le stationnement en ville, cher et contraint. Repérez celui de votre axe dès l'installation."
    ],
    impots_classes: [
      "Réclamez la fiche de retenue dès la première semaine et vérifiez que l'employeur l'a bien reçue. Sans elle, la retenue se fait au taux maximal, et la régularisation attend la déclaration suivante.",
      "Ne calez jamais un budget durable sur le net des premiers mois : il peut évoluer une fois la classe correctement appliquée."
    ],
    deductions: [
      "Ouvrez un dossier, papier ou numérique, dès la première année, et rangez-y au fil de l'eau tout justificatif potentiellement déductible. Les reconstituer en mars est une corvée.",
      "Pour la première déclaration, se faire accompagner coûte moins cher que ce que l'on oublie de déclarer."
    ],
    impatries: [
      "Le régime se demande avant la prise de poste, via l'employeur. En parler après la signature du contrat réduit fortement les chances d'aboutir.",
      "Vérifiez son application sur les premières fiches de paie : une erreur de paramétrage passe facilement inaperçue plusieurs mois."
    ],
    administration: [
      "Vérifiez la liste des pièces sur le site de votre commune, et non sur un portail national : elle varie d'une commune à l'autre pour une même démarche.",
      "Demandez le nom de la personne qui traite votre dossier. Dans un petit pays, pouvoir rappeler quelqu'un de précis change tout."
    ],
    langues: [
      "Les cours communaux sont souvent moins chers et plus proches que les grandes institutions, et l'on y rencontre des voisins.",
      "Si la naturalisation figure dans vos projets, commencez le luxembourgeois dès la première année : le niveau demandé se travaille sur la durée."
    ],
    dechets: [
      "Récupérez le calendrier de collecte le jour de l'emménagement et notez les jours dans votre agenda. Les rythmes diffèrent d'une commune à l'autre.",
      "Repérez les horaires du centre de recyclage avant le déménagement : c'est le moment où l'on produit le plus de cartons."
    ],
    independant: [
      "Vérifiez l'autorisation d'établissement avant d'accepter la première mission, pas avant la première facture. Certaines activités demandent des semaines d'instruction.",
      "Si un client exige un numéro d'affiliation pour vous payer, la dispense pour revenu insignifiant ne convient pas : elle n'en produit aucun."
    ],
    emploi: [
      "Indiquez votre niveau réel dans chaque langue, sans le gonfler : les entretiens basculent souvent d'une langue à l'autre sans prévenir.",
      "Les postes déclarés au service public de l'emploi ne sont pas tous publiés ailleurs : l'inscription vaut le détour même en poste."
    ],
    conges: [
      "Demandez la convention collective applicable avant de signer : elle accorde souvent plusieurs jours de congé au-delà du minimum légal.",
      "Vérifiez la présence d'un treizième mois, qui n'est pas obligatoire, et comparez les offres sur le brut annuel total."
    ],
    logement_abordable: [
      "Vérifiez la condition de non-propriété avant de constituer le dossier : un bien conservé dans le pays d'origine, même loué, disqualifie tout le ménage et rend le reste inutile.",
      "Si vous hébergez un ascendant, sachez que cela n'élève pas le plafond mais ajoute ses revenus au calcul. Faites la simulation avant de vous engager.",
      "Rappelez-vous que la revente est encadrée : ce type de bien se choisit pour y vivre longtemps, pas comme première étape patrimoniale."
    ],
    interets: [
      "Refaites le calcul à partir de la troisième année : c'est le moment où le plafond commence à mordre, et où la mensualité paraît soudain moins avantageuse.",
      "Comptez tous les membres du ménage dans le plafond, conjoint et enfants compris. Beaucoup ne déclarent que le montant par personne et perdent la moitié de la déduction."
    ],
    allocations: [
      "La demande initiale se fait une seule fois, mais elle ne se déclenche pas toute seule : tant qu'elle n'est pas déposée, rien n'arrive.",
      "Signalez tout changement de situation, naissance ou déménagement, sans attendre. Les régularisations rétroactives sont plus pénibles que la mise à jour."
    ],
    cout_vie: [
      "Comparez toujours les offres d'emploi sur le brut annuel, treizième mois compris, et non sur le salaire mensuel affiché.",
      "Intégrez le logement dès le début du calcul : c'est lui qui absorbe l'essentiel de l'écart de salaire avec les régions voisines.",
      "Une tranche indiciaire peut tomber en cours d'année et revaloriser salaires et plafonds : un montant lu il y a un an n'est probablement plus le bon."
    ],
    sante_pratique: [
      "Cherchez un médecin, et un pédiatre si vous avez des enfants, dès l'installation. Beaucoup n'acceptent plus de nouveaux patients, et la recherche prend du temps.",
      "Enregistrez le 112 et la liste des pharmacies de garde de votre commune avant d'en avoir besoin, un soir de week-end.",
      "Comme vous avancez les frais, gardez un moyen de paiement disponible pour les premières consultations."
    ],
    assurance_auto: [
      "Souscrivez avant l'immatriculation, pas après : l'attestation est exigée au guichet.",
      "Comparez la franchise et l'assistance plutôt que la prime. Le jour du sinistre, c'est le remorquage et le véhicule de remplacement que vous constaterez, pas les dix euros d'écart mensuel."
    ],
    nationalite: [
      "Le volet linguistique est le plus long à préparer : si la naturalisation vous intéresse, commencez les cours des la première année, sans attendre de remplir la condition de résidence.",
      "Conservez soigneusement les preuves de résidence continue au fil des années : elles seront demandées, et se reconstituent mal."
    ]
  };

  var INTITULE = "Astuces de la communauté";
  var AVERTISSEMENT = "Retours d'expérience de personnes déjà installées. Ces conseils n'ont pas de valeur officielle : en cas d'enjeu, la source citée plus haut fait foi.";

  // Style injecté, pour ne pas dépendre de la feuille principale.
  function injecterStyle() {
    if (document.getElementById("style-communaute")) return;
    var st = document.createElement("style");
    st.id = "style-communaute";
    st.textContent = [
      ".communaute{margin:34px 0 8px;padding:20px 22px;border-radius:16px;",
      "background:color-mix(in srgb, var(--accent-3, #6b4df6) 7%, var(--surface, #fff));",
      "border:1px solid color-mix(in srgb, var(--accent-3, #6b4df6) 26%, transparent);}",
      ".communaute .titre-c{display:flex;align-items:center;gap:9px;margin-bottom:6px;",
      "font-size:11px;font-weight:680;letter-spacing:1.1px;text-transform:uppercase;",
      "color:var(--accent-3, #6b4df6);}",
      ".communaute .titre-c .pastille{width:7px;height:7px;border-radius:50%;",
      "background:var(--accent-3, #6b4df6);flex:none;}",
      ".communaute .avert{margin:0 0 14px;font-size:12.5px;color:var(--muted, #6a7583);line-height:1.55;}",
      ".communaute ul{margin:0;padding-left:20px;}",
      ".communaute li{margin:9px 0;color:var(--text-2, #38424f);font-size:15.5px;line-height:1.65;}"
    ].join("");
    document.head.appendChild(st);
  }

  function bloc(astuces) {
    var d = document.createElement("div");
    d.className = "communaute";

    var t = document.createElement("div");
    t.className = "titre-c";
    var p = document.createElement("span");
    p.className = "pastille";
    t.appendChild(p);
    t.appendChild(document.createTextNode(INTITULE));
    d.appendChild(t);

    var a = document.createElement("p");
    a.className = "avert";
    a.textContent = AVERTISSEMENT;
    d.appendChild(a);

    var ul = document.createElement("ul");
    astuces.forEach(function (x) {
      var li = document.createElement("li");
      li.textContent = x;
      ul.appendChild(li);
    });
    d.appendChild(ul);
    return d;
  }

  // Identifie la fiche ouverte à partir de l'ancre, puis insère le bloc
  // à la fin de la colonne de texte.
  function poser() {
    var h = (window.location.hash || "").replace("#", "");
    if (h.indexOf("fiche/") !== 0) return;
    var id = h.slice(6);
    var astuces = ASTUCES[id];
    if (!astuces || !astuces.length) return;

    var texte = document.querySelector("#fiche-detail .detail-texte");
    if (!texte) return;
    if (texte.querySelector(".communaute")) return;
    texte.appendChild(bloc(astuces));
  }

  function demarrer() {
    injecterStyle();
    poser();
    var cible = document.getElementById("fiche-detail");
    if (cible && "MutationObserver" in window) {
      new MutationObserver(function () { poser(); }).observe(cible, { childList: true, subtree: true });
    }
    window.addEventListener("hashchange", function () { setTimeout(poser, 30); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", demarrer);
  } else {
    demarrer();
  }

  // Exposé pour l'onglet Administration et pour d'éventuels traitements.
  window.COMMUNAUTE = { astuces: ASTUCES, intitule: INTITULE };
})();
