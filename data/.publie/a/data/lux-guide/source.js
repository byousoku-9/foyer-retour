// Base de connaissances : s'installer au Luxembourg.
// Contenu générique et anonyme. Aucune donnée personnelle.
// Rédaction originale, sources officielles citées par fiche. Vérification : août 2026.
//
// Corps d'une fiche : une chaîne = un paragraphe, { h: "..." } = un sous-titre.
// Étapes du parcours : { t: texte, fiche: identifiant, si: condition de profil }.

window.KB = {
  meta: { verifie: "2026-08", devise: "EUR", version: 3 },

  fiches: [
    {
      id: "arrivee",
      titre: "Les huit premiers jours",
      cat: "Administratif",
      resume: "Tout part de la commune. Sans cette déclaration, ni matricule, ni sécurité sociale, ni banque, ni école.",
      tags: ["arrivée", "commune", "déclaration", "déménagement", "premiers jours", "enregistrement", "biergercenter", "certificat de résidence"],
      corps: [
        "La première démarche après l'emménagement n'est ni la banque ni l'employeur : c'est la commune. Vous disposez de huit jours pour déclarer votre arrivée au bureau de la population, souvent appelé Biergercenter. Cette formalité, qui prend une vingtaine de minutes, conditionne littéralement tout le reste de votre installation.",
        "Elle produit en effet deux documents que l'on vous redemandera partout : le certificat de résidence et, pour les ressortissants de l'Union restant plus de trois mois, l'attestation d'enregistrement.",
        { h: "Le matricule, clé de tout le reste" },
        "Le certificat porte votre numéro d'identification national, communément appelé matricule. Ce numéro à treize chiffres suit une logique simple : votre date de naissance inversée, au format année, mois, jour, suivie de cinq chiffres. Il vous servira d'identifiant auprès de la sécurité sociale, de l'administration fiscale, de votre banque, de l'école et de la commune elle-même. Notez-le dès que vous l'avez, vous le taperez souvent.",
        { h: "Ce qu'il faut apporter" },
        "Prévoyez les originaux des pièces d'identité de chaque membre du foyer, le bail signé ou l'acte d'achat, et, selon votre situation familiale, les actes de naissance et de mariage. Certaines communes exigent des documents récents ou traduits : vérifiez leurs demandes avant de vous déplacer, cela évite un second rendez-vous.",
        "La plupart des communes fonctionnent désormais sur rendez-vous, à prendre en ligne. En période de rentrée, les créneaux partent vite, ce qui peut suffire à faire dépasser le délai de huit jours. Réservez avant même d'avoir les clés."
      ],
      aRetenir: [
        "Huit jours après l'emménagement, pas après la signature du bail.",
        "Le matricule vient de la commune, pas de la sécurité sociale.",
        "Prendre rendez-vous en ligne avant l'arrivée."
      ],
      sources: [{ t: "Guichet.lu, citoyenneté et démarches d'arrivée", u: "https://guichet.public.lu/fr/citoyens/citoyennete.html" }]
    },
    {
      id: "matricule",
      titre: "Sécurité sociale et remboursements",
      cat: "Sante",
      resume: "L'affiliation est automatique pour un salarié. Le système fonctionne par avance de frais.",
      tags: ["matricule", "cns", "sécurité sociale", "ccss", "carte", "affiliation", "remboursement", "médecin", "mutuelle"],
      corps: [
        "Une fois le matricule attribué, l'affiliation à la sécurité sociale se fait sans démarche de votre part si vous êtes salarié : c'est l'employeur qui déclare votre entrée au Centre commun de la sécurité sociale. Votre carte arrive ensuite par courrier, et sera demandée chez le médecin comme en pharmacie. Les travailleurs indépendants, eux, doivent déclarer eux-mêmes leur activité, dans un délai de huit jours.",
        { h: "Payer d'abord, être remboursé ensuite" },
        "Le principe surprend souvent les nouveaux arrivants : vous réglez la consultation intégralement, puis la Caisse nationale de santé vous rembourse, généralement entre 80 et 88 % du tarif conventionné. Ce remboursement suppose que la caisse dispose de vos coordonnées bancaires, communiquées lors de la première demande. Tout changement de compte doit ensuite être signalé par écrit, en mentionnant votre matricule.",
        "Concrètement, prévoyez une petite trésorerie les premières semaines, le temps que le circuit s'amorce. Une famille qui enchaîne les visites médicales à l'arrivée peut avancer plusieurs centaines d'euros avant le premier remboursement.",
        { h: "Faut-il une complémentaire ?" },
        "La couverture obligatoire laisse un reste à charge sur l'hospitalisation en chambre individuelle, les soins dentaires lourds et l'optique. Une assurance complémentaire privée est donc courante, proposée par les assureurs locaux comme par certaines mutuelles. Comparez les plafonds annuels plutôt que les seules cotisations : c'est là que les contrats se différencient réellement."
      ],
      aRetenir: [
        "Salarié : rien à faire, l'employeur s'en charge.",
        "Indépendant : déclaration au CCSS sous huit jours.",
        "Prévoir la trésorerie de l'avance de frais."
      ],
      sources: [
        { t: "Centre commun de la sécurité sociale", u: "https://ccss.public.lu/fr.html" },
        { t: "Caisse nationale de santé", u: "https://cns.public.lu/fr.html" }
      ]
    },
    {
      id: "assurance_sante",
      titre: "La complémentaire santé",
      cat: "Sante",
      resume: "Facultative, mais elle couvre précisément ce que la caisse laisse de côté : chambre seule, dentaire et optique.",
      tags: ["complémentaire", "mutuelle", "santé", "hospitalisation", "dentaire", "optique", "carence", "cns", "assurance"],
      corps: [
        "L'assurance maladie obligatoire couvre l'essentiel des soins, mais laisse un reste à charge concentré sur trois postes : l'hospitalisation en chambre individuelle, les soins dentaires lourds et l'optique. C'est précisément ce que vient couvrir une complémentaire privée, proposée par les assureurs locaux comme par des mutuelles.",
        "Elle n'a rien d'obligatoire, et son utilité dépend de votre situation. Une personne jeune et sans besoin dentaire particulier peut s'en passer plusieurs années. Une famille avec des enfants à appareiller, ou toute personne attachée à la chambre seule en cas d'hospitalisation, l'amortit rapidement.",
        { h: "Le délai de carence, le point à vérifier en premier" },
        "La plupart des contrats prévoient un délai de carence, c'est-à-dire une période initiale pendant laquelle certaines garanties ne jouent pas encore, souvent plusieurs mois pour le dentaire et parfois davantage pour l'orthodontie. Souscrire à l'arrivée, avant d'en avoir besoin, évite de découvrir cette clause au mauvais moment. Certains contrats prévoient également des questionnaires médicaux ou des exclusions pour les affections antérieures : lisez cette partie avant de signer.",
        { h: "Comparer sur les plafonds, pas sur la cotisation" },
        "Deux contrats affichant la même cotisation mensuelle peuvent différer du simple au double sur ce qu'ils remboursent. Regardez le plafond annuel par poste, en particulier pour le dentaire et l'optique, le taux de remboursement de la chambre seule et sa durée maximale, et l'existence d'un plafond global tous postes confondus.",
        "Vérifiez également si le contrat couvre les soins reçus à l'étranger. C'est loin d'être anecdotique pour une famille récemment installée, qui continue souvent de consulter dans son pays d'origine les premières années.",
        { h: "Une particularité à connaître" },
        "Contrairement aux assurances habitation ou automobile, les complémentaires santé ne sont pas couvertes par le document d'information standardisé européen. Vous ne disposerez donc pas de la fiche synthétique de deux pages qui facilite la comparaison ailleurs : il faut lire les conditions générales et le tableau de garanties."
      ],
      aRetenir: [
        "Souscrire à l'arrivée, à cause des délais de carence.",
        "Comparer les plafonds par poste, pas la cotisation.",
        "Vérifier la couverture des soins reçus à l'étranger."
      ],
      sources: [
        { t: "Caisse nationale de santé", u: "https://cns.public.lu/fr.html" },
        { t: "Commissariat aux assurances", u: "https://www.caa.lu/fr/accueil" }
      ]
    },
    {
      id: "administration",
      titre: "Comment fonctionne l'administration",
      cat: "Administratif",
      resume: "Petit pays, circuits courts et interlocuteurs accessibles. Mais le courrier et la présence physique restent la règle pour certaines démarches.",
      tags: ["administration", "démarches", "guichet", "délai", "rendez-vous", "courrier", "langue", "commune", "efficacité"],
      corps: [
        "L'administration luxembourgeoise surprend agréablement la plupart des nouveaux arrivants, en particulier ceux venus de grands pays. Le pays est petit, les circuits sont courts, et il n'est pas rare d'obtenir en une visite ce qui demanderait ailleurs plusieurs semaines et trois formulaires.",
        { h: "Ce qui va vite" },
        "Beaucoup de documents sont délivrés immédiatement au guichet : certificat de résidence, attestation d'enregistrement, copies conformes. Les communes fonctionnent largement sur rendez-vous, ce qui évite les files d'attente, et les agents répondent en français, en allemand, souvent en anglais.",
        "Les délais de traitement des dossiers plus lourds restent raisonnables, et surtout, les administrations sont joignables. Un appel ou un courriel obtient généralement une réponse d'une personne identifiable, ce qui change la nature des échanges : en cas de difficulté, il est presque toujours possible de parler à quelqu'un qui connaît le dossier.",
        { h: "Ce qui reste lent, ou papier" },
        "Tout n'est pas dématérialisé pour autant. Certaines démarches ne se traitent que par courrier postal ou en personne, l'enregistrement d'un permis de conduire étranger en étant l'exemple le plus courant. D'autres exigent un certificat obtenu ailleurs, ce qui crée des chaînes de dépendance : sans déclaration à la commune, pas de certificat de résidence, donc pas de chèque-service ni d'inscription scolaire.",
        "Ce sont ces enchaînements, plus que la lenteur des services, qui allongent une installation. D'où l'intérêt de connaître l'ordre des démarches, plutôt que de les entreprendre au fil de l'eau.",
        { h: "Trois réflexes utiles" },
        "Prenez rendez-vous en ligne dès que c'est possible, en particulier en période de rentrée, où les créneaux se remplissent plusieurs semaines à l'avance. Vérifiez la liste des pièces sur le site de votre commune, car elle varie d'une commune à l'autre pour une même démarche. Et gardez une trace écrite de ce qui vous est dit oralement : la qualité de l'accueil ne remplace pas un document daté."
      ],
      aRetenir: [
        "Beaucoup de documents sont délivrés immédiatement au guichet.",
        "Ce sont les enchaînements de démarches qui allongent l'installation, pas les délais.",
        "Prendre rendez-vous tôt, et vérifier la liste des pièces auprès de sa commune."
      ],
      sources: [{ t: "Guichet.lu", u: "https://guichet.public.lu/fr/citoyens.html" }]
    },
    {
      id: "luxtrust",
      titre: "LuxTrust et MyGuichet",
      cat: "Administratif",
      resume: "L'identité numérique qui ouvre les démarches en ligne. Bien moins chère si vous passez par votre banque.",
      tags: ["luxtrust", "myguichet", "certificat", "signature", "en ligne", "token", "identité numérique", "eid"],
      corps: [
        "LuxTrust est l'identité numérique luxembourgeoise, l'équivalent fonctionnel d'une identité électronique nationale. Elle donne accès à MyGuichet.lu, où se traitent la plupart des démarches administratives, et sert également à valider les opérations bancaires en ligne. Sans elle, vous restez cantonné aux formulaires papier et aux guichets physiques.",
        { h: "L'obtenir au meilleur prix" },
        "Le chemin le moins coûteux passe par une banque luxembourgeoise, qui la fournit souvent gratuitement à ses clients. Sans compte local, il faut la commander en direct, et elle devient payante. C'est une raison de plus pour ouvrir le compte bancaire tôt : LuxTrust suit naturellement. La carte d'identité luxembourgeoise permet aussi de s'authentifier, à condition d'avoir activé ses certificats au moment de la demande de carte.",
        { h: "Ce qui ne se fait pas en ligne" },
        "Ne présumez pas que tout est dématérialisé. L'enregistrement d'un permis de conduire étranger, par exemple, se traite exclusivement par courrier ou au guichet. À l'inverse, l'intérêt du portail dépasse le simple dépôt de dossier : il permet de suivre l'avancement d'une démarche, ce qu'un envoi postal ne permet pas."
      ],
      aRetenir: [
        "Ouvrir le compte bancaire d'abord, LuxTrust suit et coûte moins cher.",
        "Le portail permet de suivre l'état d'un dossier.",
        "Certaines démarches restent papier, dont le permis de conduire."
      ],
      sources: [{ t: "MyGuichet.lu", u: "https://guichet.public.lu/fr/citoyens/support/aide/myguichet.html" }]
    },
    {
      id: "recherche_logement",
      titre: "Trouver un logement",
      cat: "Logement",
      resume: "Un marché tendu, où un dossier complet et une garantie déjà disponible font la différence.",
      tags: ["chercher", "logement", "recherche", "annonce", "agence", "visite", "dossier", "commune", "loyer"],
      corps: [
        "Le marché locatif est tendu, en particulier dans la capitale et sa première couronne. Les biens intéressants partent en quelques jours, et le candidat retenu n'est pas toujours celui qui paie le plus : c'est souvent celui dont le dossier est complet et immédiatement exploitable.",
        { h: "Préparer le dossier avant de visiter" },
        "Constituez à l'avance un dossier unique : pièces d'identité, contrat de travail ou promesse d'embauche, trois derniers bulletins de salaire, et coordonnées bancaires. Ajoutez, si vous l'avez déjà, la confirmation que votre banque peut émettre la garantie locative. C'est précisément le point qui bloque les candidats étrangers, et celui qui vous distinguera.",
        { h: "Où chercher" },
        "Les portails d'annonces généralistes concentrent l'essentiel de l'offre, mais une partie du marché circule par les agences, par les groupes d'expatriés et par le bouche-à-oreille des employeurs. Les grandes entreprises et les institutions disposent souvent d'un service ou d'un prestataire d'accompagnement : demandez-le, c'est fréquemment inclus dans le package d'embauche.",
        { h: "Élargir au-delà de la capitale" },
        "Le prix baisse nettement dès que l'on s'éloigne de Luxembourg-ville, mais la variable décisive n'est pas la distance : c'est la desserte. Une commune située à vingt kilomètres sur un axe ferroviaire peut être plus rapide d'accès qu'une commune à dix kilomètres mal reliée. Vérifiez les horaires réels depuis l'arrêt le plus proche, aux heures qui vous concernent, avant de vous décider.",
        "Anticipez enfin l'enchaînement des délais : ouverture du compte bancaire, puis garantie, puis signature, puis état des lieux. Entre la première visite et la remise des clés, comptez rarement moins de trois à quatre semaines."
      ],
      aRetenir: [
        "Dossier complet prêt avant la première visite.",
        "La garantie bancaire est le point bloquant des candidats étrangers.",
        "Juger une commune sur sa desserte, pas sur sa distance."
      ],
      sources: [{ t: "luxembourg.public.lu, se loger", u: "https://luxembourg.public.lu/fr/vivre.html" }]
    },
    {
      id: "choisir_commune",
      titre: "Choisir sa commune",
      resume: "Le bon critère n'est pas la distance, mais votre mode de déplacement et les trajets que vous ferez tous les jours.",
      cat: "Logement",
      tags: ["commune", "quartier", "choisir", "distance", "bus", "train", "école", "trajet", "voiture", "transports"],
      corps: [
        "Beaucoup de nouveaux arrivants choisissent leur commune sur une carte, en traçant un rayon autour du lieu de travail. C'est le meilleur moyen de se tromper : deux communes situées à la même distance peuvent offrir des quotidiens radicalement différents selon la desserte et selon les trajets réellement effectués chaque jour.",
        { h: "Première question : voiture ou transports ?" },
        "Si vous vous déplacez en voiture, la distance pèse peu et vous pouvez élargir largement la zone de recherche, ce qui fait baisser le loyer de façon significative. Regardez alors le temps de trajet aux heures de pointe, et non le kilométrage : les axes vers la capitale saturent, et vingt kilomètres peuvent prendre quarante-cinq minutes le matin. Vérifiez aussi le stationnement, à la fois au logement et sur le lieu de travail, souvent cher et contingenté.",
        "Si vous comptez sur les transports publics, la logique s'inverse totalement. La gratuité ne sert à rien si aucune ligne ne passe. Ouvrez les horaires réels depuis l'arrêt le plus proche du logement visité, aux heures qui vous concernent, y compris le soir et le week-end. Une commune desservie toutes les dix minutes en semaine peut n'avoir qu'un bus par heure le samedi.",
        { h: "Deuxième question : les trajets contraints" },
        "Le trajet domicile-travail est rarement le plus contraignant pour une famille. Celui qui structure vraiment les journées, c'est l'école, et surtout la garde, parce qu'il a lieu deux fois par jour à heure fixe. Si les enfants sont scolarisés dans un établissement précis, une école européenne ou une section linguistique donnée, cherchez le logement autour de cette école, pas autour du bureau.",
        "Faites le test concrètement, avant de signer : simulez une journée complète, dépose à l'école, trajet vers le travail, retour, récupération à la garde. Si l'enchaînement ne tient pas sur le papier, il ne tiendra pas en pratique.",
        { h: "Les critères qu'on oublie" },
        "Repérez la distance à pied jusqu'à l'arrêt de bus ou à la gare, et pas seulement leur existence : dix minutes de marche sous la pluie deux fois par jour changent l'expérience. Vérifiez la présence d'un parking relais si vous combinez voiture et train. Regardez enfin les commerces de proximité, la crèche communale et le centre de recyclage, ces trois adresses que vous fréquenterez plus que vous ne le pensez.",
        "Un dernier réflexe utile : la commune est aussi votre guichet administratif pour l'enregistrement, le chèque-service et les inscriptions scolaires. Ses horaires d'ouverture et sa réactivité font partie du confort de vie."
      ],
      aRetenir: [
        "En voiture, élargir la zone et regarder le temps aux heures de pointe.",
        "En transports, vérifier les horaires réels depuis l'arrêt le plus proche.",
        "Chercher autour de l'école, pas autour du bureau."
      ],
      sources: [{ t: "mobiliteit.lu, horaires et itinéraires", u: "https://www.mobiliteit.lu/" }]
    },
    {
      id: "rdv_technique",
      titre: "Réussir un rendez-vous technique",
      cat: "Quotidien",
      resume: "Installations et raccordements échouent souvent pour une adresse mal transmise ou un appel manqué. Quelques précautions évitent des semaines de retard.",
      tags: ["rendez-vous", "technicien", "installation", "raccordement", "fibre", "interphone", "adresse", "absence", "frais"],
      corps: [
        "Le raccordement à internet, l'installation d'un compteur ou une intervention sur le réseau supposent la venue d'un technicien, et donc votre présence. Ces rendez-vous échouent plus souvent qu'on ne l'imagine, rarement pour des raisons techniques : le plus souvent, le technicien n'a pas trouvé la porte, ou n'a pas pu vous joindre. Chaque échec coûte plusieurs semaines, et parfois des frais de déplacement.",
        { h: "L'adresse exacte, et ce qui figure sur l'interphone" },
        "Dans un immeuble récent, l'adresse du logement et celle du local technique diffèrent parfois : le boîtier peut se trouver à un autre numéro de rue du même ensemble. Précisez donc les deux, par écrit, avant l'intervention. Indiquez également le nom inscrit sur l'interphone, qui n'est pas toujours celui du contrat, en particulier dans un couple aux noms différents ou lors d'une reprise de bail. Ajoutez l'étage, le bloc et le numéro d'appartement.",
        "Faites-le par un canal qui laisse une trace : messagerie du portail de suivi, ou courriel. Un message oral au téléphone ne se retrouve pas, et ne vous servira à rien si vous devez contester ensuite.",
        { h: "Se rendre joignable, vraiment" },
        "Beaucoup de techniciens appellent depuis un numéro masqué. Un appel masqué ne peut être ni identifié, ni rappelé : dites-le explicitement au moment de la prise de rendez-vous, et demandez un numéro sur lequel vous pouvez rappeler. Donnez un second numéro si quelqu'un d'autre est présent sur place, et vérifiez que sonnette et interphone fonctionnent la veille.",
        { h: "Faire confirmer le créneau par écrit" },
        "Un rendez-vous fixé ou déplacé par téléphone ne laisse aucune trace. Demandez systématiquement une confirmation écrite de la date et du créneau horaire. C'est ce document qui vous protégera si le technicien se présente en dehors de la plage annoncée, ce qui arrive régulièrement.",
        { h: "Quand deux intervenants sont nécessaires" },
        "Certains raccordements exigent le passage successif de deux entreprises différentes, l'opérateur de réseau puis votre fournisseur. Elles ne sont pas toujours coordonnées, et l'échec de la première rend la seconde inutile. Demandez qui doit venir, dans quel ordre, et ce qui se passe si le premier passage échoue.",
        { h: "Si des frais d'absence vous sont facturés" },
        "Lisez la fiche d'intervention avant de payer. Elle mentionne souvent une rubrique de responsabilité, et il n'est pas rare qu'elle indique que le client n'est pas en cause tout en facturant des frais d'absence : la contradiction est un argument solide. Vérifiez aussi l'adresse portée sur la fiche, et l'heure des appels reçus. Contestez par écrit, en joignant vos captures d'écran."
      ],
      aRetenir: [
        "Signaler par écrit l'adresse exacte, l'étage et le nom sur l'interphone.",
        "Prévenir qu'un appel masqué ne peut pas être rappelé.",
        "Exiger la confirmation écrite de la date et du créneau."
      ],
      sources: [{ t: "ILR, service de médiation", u: "https://www.ilr.lu/mediation/" }]
    },
    {
      id: "reclamation",
      titre: "Contester une facture ou un service",
      cat: "Quotidien",
      resume: "Une réclamation écrite est le préalable obligatoire à toute médiation. Un appel téléphonique, même suivi d'un refus, ne compte pas.",
      tags: ["réclamation", "litige", "médiation", "facture", "contester", "remboursement", "ilr", "consommateur", "preuve"],
      corps: [
        "Que le désaccord porte sur une facture de télécoms, une prestation d'énergie ou un service financier, la mécanique est toujours la même, et elle commence par un écrit.",
        { h: "Écrire d'abord, toujours" },
        "Les organismes de médiation exigent, comme condition de recevabilité, une réclamation écrite préalable adressée au professionnel, restée sans réponse ou suivie d'une réponse insatisfaisante. Un échange téléphonique, même lorsqu'un conseiller vous a explicitement opposé un refus, ne remplit pas cette condition et ne laisse aucune trace. Écrivez donc systématiquement, même après un appel, ne serait-ce que pour acter par écrit ce qui vous a été dit.",
        { h: "Ce qui rend une réclamation efficace" },
        "Une réclamation convaincante tient en quelques éléments : des faits datés et horodatés, les pièces qui les établissent, et une demande précise. Évitez d'empiler les griefs : un argument documenté vaut mieux que cinq affirmations. Les meilleurs arguments sont souvent ceux que le professionnel a lui-même produits, un compte rendu d'intervention, un accusé de lecture, un horodatage.",
        "Fixez un délai de réponse raisonnable, une à deux semaines, et annoncez la suite si rien ne vient. Cette annonce n'est pas une menace, c'est le déroulé normal de la procédure, et elle est souvent suffisante.",
        { h: "Constituer ses preuves au fil de l'eau" },
        "Conservez tout au moment où cela se produit, et non après coup : captures d'écran des messages et de leur horodatage, journal des appels, photographies, courriels. Ces éléments sont faciles à réunir sur le moment, et quasiment impossibles à reconstituer trois semaines plus tard.",
        { h: "Vers qui se tourner ensuite" },
        "Pour les communications électroniques, la médiation relève de l'Institut luxembourgeois de régulation, et elle est gratuite. Pour les autres litiges de consommation, le service national du Médiateur de la consommation est compétent. Dans les deux cas, la réclamation écrite et l'éventuelle réponse du professionnel font partie des pièces obligatoires du dossier."
      ],
      aRetenir: [
        "Un appel téléphonique ne vaut pas réclamation écrite.",
        "Les meilleurs arguments sont les documents du professionnel lui-même.",
        "Constituer les preuves le jour même, pas trois semaines après."
      ],
      sources: [
        { t: "ILR, service de médiation", u: "https://www.ilr.lu/mediation/" },
        { t: "Médiateur de la consommation", u: "https://www.mediateurconsommation.lu/" }
      ]
    },
    {
      id: "emmenagement",
      titre: "Emménager et meubler",
      cat: "Logement",
      resume: "Mesurer l'ascenseur avant de commander, prévoir la montée, et acheter des luminaires : le logement se loue souvent sans.",
      tags: ["emménagement", "meuble", "livraison", "ascenseur", "montage", "luminaire", "lumière", "déménagement", "achat"],
      corps: [
        "Les mauvaises surprises de l'emménagement sont rarement dramatiques, mais elles coûtent du temps et de l'argent. Trois d'entre elles reviennent systématiquement.",
        { h: "Mesurer avant de commander" },
        "Les meubles en kit sont livrés dans des cartons dont la longueur peut dépasser la profondeur d'une cabine d'ascenseur. Une armoire haute, par exemple, se vend souvent en deux hauteurs, et seule la plus petite passe dans un ascenseur d'immeuble courant. Mesurez la cabine, la porte palière et les paliers d'escalier avant de commander, pas après. Vérifiez également la largeur des portes intérieures pour les meubles livrés montés.",
        { h: "La livraison s'arrête souvent au rez-de-chaussée" },
        "Beaucoup de livreurs déposent au pied de l'immeuble, ou au premier obstacle. La montée et le montage sont alors à votre charge, et il faut soit s'en occuper, soit recourir à un service d'aide à domicile ou à un monteur indépendant. Anticipez ce poste dans le budget, et vérifiez ce que couvre exactement l'option de livraison proposée.",
        { h: "Le logement se loue souvent sans luminaires" },
        "Beaucoup de logements sont remis avec des fils qui pendent au plafond et aucun point lumineux monté. Ce n'est pas un défaut ni un oubli du bailleur, c'est l'usage courant ici, alors que dans plusieurs pays voisins une douille équipée reste en place. Prévoyez les luminaires et leur pose dès le premier jour, sans quoi les premières soirées se passent à la lampe de chantier. Le point est à vérifier à l'état des lieux, pas après.",
        { h: "Acheter de part et d'autre de la frontière" },
        "Les enseignes de mobilier et d'électroménager sont implantées des deux côtés de la frontière, et leurs promotions ne sont pas synchronisées d'un pays à l'autre. Sur un achat conséquent, comparer les prix dans les magasins voisins de Belgique, de France ou d'Allemagne vaut souvent le déplacement, à condition d'intégrer le coût de la livraison transfrontalière, parfois dissuasif."
      ],
      aRetenir: [
        "Mesurer la cabine d'ascenseur avant de commander un meuble haut.",
        "Vérifier si la livraison monte à l'étage ou s'arrête en bas.",
        "Prévoir les luminaires : ils sont rarement en place à la remise des clés."
      ],
      sources: [{ t: "Guichet.lu, bail à loyer", u: "https://guichet.public.lu/fr/citoyens/logement/location.html" }]
    },
    {
      id: "bail",
      titre: "Signer un bail",
      cat: "Logement",
      resume: "La réforme du 1er août 2024 a plafonné la caution à deux mois et partagé les frais d'agence.",
      tags: ["bail", "location", "caution", "garantie", "loyer", "agence", "préavis", "logement", "état des lieux"],
      corps: [
        "Le cadre juridique s'est nettement amélioré pour les locataires. La réforme du bail à loyer entrée en vigueur le 1er août 2024 a plafonné la garantie locative à deux mois de loyer, contre trois auparavant, et réparti les frais d'agence par moitié entre bailleur et locataire, alors qu'ils pesaient jusque-là entièrement sur le locataire. Sur un loyer de 2 000 euros, l'économie combinée dépasse facilement 3 000 euros à l'entrée dans les lieux.",
        { h: "La garantie bancaire, un piège de calendrier" },
        "La garantie prend le plus souvent la forme d'une garantie bancaire, que seule une banque luxembourgeoise émet en pratique. Un IBAN étranger, même européen, ne suffit généralement pas. Or l'ouverture d'un compte demande elle-même du temps et des justificatifs. Anticipez cette chaîne, sinon vous perdrez le logement au profit d'un candidat mieux préparé.",
        { h: "L'état des lieux décide de votre caution" },
        "Un état des lieux contradictoire est réalisé à l'entrée et à la sortie, et c'est lui qui déterminera la restitution de votre garantie deux ou trois ans plus tard. Photographiez systématiquement chaque pièce, y compris ce qui vous semble anodin, et conservez les relevés de compteurs. Ce quart d'heure d'effort vaut plusieurs milliers d'euros au moment du départ.",
        { h: "Lire les interdictions avant d'acheter des meubles" },
        "Beaucoup de baux interdisent, sans accord écrit du bailleur, les percements et modifications, la sous-location et la présence d'animaux. Vous éviterez d'acheter des meubles à fixer au mur que vous ne pourrez pas installer. Notez que ces clauses interdisent rarement de percer en soi : elles exigent un accord écrit, qu'un courriel suffit souvent à obtenir. En cas de désaccord persistant, la commission des loyers de la commune constitue le premier recours, avant toute action judiciaire."
      ],
      aRetenir: [
        "Caution : deux mois maximum depuis le 1er août 2024.",
        "Frais d'agence partagés par moitié.",
        "Photographier l'état des lieux d'entrée, sans exception."
      ],
      sources: [{ t: "Guichet.lu, bail à loyer", u: "https://guichet.public.lu/fr/citoyens/logement/location.html" }]
    },
    {
      id: "assurance_habitation",
      titre: "Assurer son logement",
      cat: "Logement",
      resume: "Exigée avant la remise des clés, et à comparer sur les garanties plutôt que sur la prime.",
      tags: ["assurance", "habitation", "responsabilité civile", "dégât des eaux", "incendie", "franchise", "conditions générales", "locataire"],
      corps: [
        "L'attestation d'assurance est demandée avant la remise des clés, aussi bien par un bailleur que par une banque en cas d'achat. Ce n'est donc pas une formalité que l'on traite après l'emménagement : elle fait partie du dossier de signature, au même titre que la garantie locative.",
        { h: "Ce que couvre réellement un contrat" },
        "Le socle habituel réunit l'incendie, les dégâts des eaux, le vol, le bris de glace et la responsabilité civile, cette dernière couvrant les dommages causés à des tiers, y compris par les enfants. Les écarts entre contrats se logent rarement dans ce socle : ils se trouvent dans les plafonds d'indemnisation, les franchises, et les exclusions.",
        { h: "Les quatre points à comparer" },
        "Regardez d'abord la franchise, c'est-à-dire ce qui reste à votre charge à chaque sinistre. Regardez ensuite si les biens sont indemnisés en valeur à neuf ou après vétusté, ce qui change tout sur du mobilier récent. Vérifiez le plafond applicable aux objets de valeur, souvent bien plus bas que le plafond général. Vérifiez enfin les exclusions liées à l'absence prolongée du logement, fréquentes et rarement lues.",
        { h: "Les documents à réclamer avant de signer" },
        "Trois documents décrivent réellement le contrat, et le devis commercial n'en fait pas partie. Le premier est le document d'information standardisé sur le produit d'assurance, deux pages au format normé dans toute l'Union, qui résume garanties, exclusions et obligations. Il facilite la comparaison entre assureurs, mais il ne remplace rien : il est volontairement succinct.",
        "Les deux autres sont contractuels. Les conditions générales décrivent l'étendue des garanties, les franchises et les exclusions applicables à tous les assurés. Les conditions particulières précisent ce qui vous concerne, montants assurés et options retenues, et elles priment sur les conditions générales en cas de divergence. Demandez les trois, et lisez en priorité les exclusions : c'est la partie qui décide de ce qui sera refusé le jour du sinistre.",
        { h: "En cas d'achat" },
        "Le prêteur exigera en plus une assurance solde restant dû, qui rembourse le crédit en cas de décès. Elle n'est pas obligatoirement souscrite auprès de la banque prêteuse : mettre plusieurs assureurs en concurrence sur ce seul poste représente souvent plusieurs milliers d'euros sur la durée du prêt."
      ],
      aRetenir: [
        "L'attestation est demandée avant la remise des clés.",
        "Comparer franchises, valeur à neuf, plafonds et exclusions.",
        "Exiger les conditions générales, pas seulement le devis."
      ],
      sources: [{ t: "Commissariat aux assurances", u: "https://www.caa.lu/fr/accueil" }]
    },
    {
      id: "achat",
      titre: "Acheter un logement",
      cat: "Logement",
      resume: "Quotités de financement encadrées, et une TVA à 3 % qui n'est ni automatique ni illimitée.",
      tags: ["achat", "acheter", "prêt", "hypothèque", "quotité", "tva", "notaire", "immobilier", "primo-accédant"],
      corps: [
        "Le financement immobilier est encadré par des quotités maximales, fixées par la CSSF sur recommandation du Comité du risque systémique. Un primo-accédant peut financer jusqu'à 100 % du prix pour sa résidence principale, contre 90 % pour un acquéreur qui a déjà été propriétaire, et 80 % seulement pour un investissement locatif. Ces pourcentages portent sur le prix du bien : les frais d'acte restent, dans tous les cas, à financer sur fonds propres.",
        { h: "La TVA à 3 % : conditionnelle et plafonnée" },
        "Le taux super-réduit de 3 %, au lieu de 17 %, est l'un des avantages les plus commentés du marché luxembourgeois. Il est aussi le plus mal compris. Il suppose d'abord une affectation à l'habitation principale pendant au moins deux ans, et l'accord préalable de l'Administration de l'enregistrement, d'où la mention « sous réserve d'acceptation » qui figure sur les annonces.",
        "Surtout, l'avantage est plafonné à 50 000 euros par logement. Comme l'écart entre 17 % et 3 % représente quatorze points, ce plafond couvre environ 357 000 euros de base hors taxe. Au-delà, la part restante repasse à 17 %. Sur un bien neuf à prix élevé, une fraction du prix supporte donc le taux plein, ce qui peut représenter plusieurs dizaines de milliers d'euros.",
        "La question à poser au promoteur est donc précise : le prix affiché intègre-t-il déjà cette part à 17 %, ou suppose-t-il 3 % sur la totalité ? Certaines annonces mentionnent honnêtement « TVA à 3 % et 17 % comprises », d'autres non. Un crédit d'impôt sur les droits d'enregistrement, connu sous le nom de Bëllegen Akt, existe par ailleurs pour l'acquisition d'une habitation principale ; son montant évolue, vérifiez-le à la date de votre projet."
      ],
      aRetenir: [
        "Primo-accédant : jusqu'à 100 % du prix, hors frais.",
        "Investissement locatif : 80 % maximum, donc 20 % d'apport.",
        "TVA à 3 % plafonnée à 50 000 euros d'avantage, puis 17 %."
      ],
      sources: [
        { t: "Guichet.lu, TVA logement", u: "https://guichet.public.lu/fr/citoyens/aides/logement-construction/aides-indirectes/remboursement-tva-taux-reduit.html" },
        { t: "Comité du risque systémique, quotités", u: "https://gouvernement.lu/fr/actualites/toutes_actualites/communiques/2020/11-novembre/18-comite-risque-cssf.html" }
      ]
    },
    {
      id: "logement_abordable",
      titre: "Logement abordable et à coût modéré",
      cat: "Logement",
      resume: "Un prix sous le marché, en échange de plafonds de revenus et d'une revente encadrée.",
      tags: ["abordable", "coût modéré", "éligibilité", "plafond", "certificat", "social", "aide", "logabo"],
      corps: [
        "Des promoteurs publics commercialisent des logements à un prix inférieur au marché. L'accès suppose un certificat d'éligibilité délivré par le Guichet unique des aides au logement, valable pour l'année de son émission et au moins six mois. Deux conditions doivent être réunies simultanément, et la première élimine plus de candidats que la seconde.",
        { h: "Ne posséder aucun autre logement" },
        "Ni vous, ni aucune personne vivant sous votre toit, ne devez être propriétaire, usufruitier, emphytéote ou titulaire d'un droit d'habitation portant sur plus d'un tiers indivis d'un autre logement, au Luxembourg comme à l'étranger, au plus tard neuf mois après l'acte. Un appartement conservé dans le pays d'origine, même loué, suffit à disqualifier tout le ménage.",
        { h: "Rester sous le plafond de revenus" },
        "Le revenu retenu est celui de l'ensemble de la communauté domestique, c'est-à-dire de toutes les personnes vivant dans le logement. Seuls les revenus des enfants à charge en sont exclus. Depuis 2026, le calcul intègre l'ensemble des revenus, y compris ceux qui ne sont pas imposables au Luxembourg.",
        "Une conséquence contre-intuitive mérite d'être signalée : héberger un parent âgé n'élève pas le plafond, qui ne progresse qu'avec les enfants à charge, mais ajoute ses revenus au calcul et importe son éventuel patrimoine immobilier dans la condition de non-propriété.",
        { h: "Une revente à prix encadré" },
        "Ces logements sont assortis d'un droit de rachat inscrit dans l'acte authentique. Le prix de revente correspond au prix payé, réindexé sur l'indice du coût de la construction et minoré de l'usure. Autrement dit, il n'y a pas de plus-value de marché à espérer : c'est un logement pour habiter, pas un placement."
      ],
      tableaux: [{
        titre: "Plafonds de revenu annuel net du ménage",
        colonnes: ["Composition du ménage", "Vente abordable", "Vente à coût modéré"],
        lignes: [
          ["1 adulte sans enfant à charge", "61 422,07 €", "72 339,21 €"],
          ["Ménage sans enfant à charge", "92 137,91 €", "108 518,41 €"],
          ["Avec 1 enfant à charge", "116 708,66 €", "137 448,34 €"],
          ["Avec 2 enfants à charge", "141 279,41 €", "166 387,86 €"],
          ["Avec 3 enfants à charge", "165 850,16 €", "195 327,38 €"],
          ["Par enfant supplémentaire", "+ 18 425,66 €", "+ 21 699,84 €"]
        ]
      }],
      aRetenir: [
        "Un bien possédé à l'étranger disqualifie tout le ménage.",
        "Le plafond ne progresse qu'avec les enfants à charge.",
        "Revente à prix encadré : pour habiter, pas pour investir."
      ],
      sources: [{ t: "Logement.lu, certificat d'éligibilité", u: "https://logement.public.lu/fr/proprietaire/obtenir-aide-achat-construction/certificat-eligibilite-logabo.html" }]
    },
    {
      id: "impots_classes",
      titre: "Classes d'impôt",
      cat: "Impots",
      resume: "La classe détermine le barème. À revenu égal, l'écart entre classe 1 et classe 2 dépasse souvent mille euros par mois.",
      tags: ["impôt", "classe", "1a", "barème", "splitting", "marié", "célibataire", "fiche de retenue", "imposition"],
      corps: [
        "Trois classes d'imposition coexistent, et la vôtre pèse davantage sur votre net que bien des négociations salariales. La classe 1 concerne les célibataires sans enfant à charge. La classe 1a vise les parents isolés, les veufs et les personnes de plus de soixante-quatre ans : elle applique un barème intermédiaire, dont l'avantage se réduit à mesure que le revenu augmente. La classe 2, enfin, s'applique aux couples mariés ou partenaires imposés collectivement, avec un mécanisme de splitting qui allège fortement l'impôt.",
        { h: "Quand les deux conjoints travaillent" },
        "C'est le cas le plus fréquent, et le plus mal compris. Un ménage imposé collectivement ne reçoit qu'une seule fiche de retenue principale, portée par usage par la rémunération la plus élevée et la plus stable. Le second salaire relève d'une fiche additionnelle, sur laquelle l'administration inscrit un taux fixe : 15 % en classe 2, 21 % en classe 1a, 33 % en classe 1.",
        "Point essentiel : ce taux ne dépend que de la classe d'impôt, jamais du revenu réel du ménage. Votre employeur ne connaît donc pas le salaire de votre conjoint, et ne peut pas le déduire du taux qui lui est communiqué. Il applique simplement ce que l'administration lui transmet, désormais par voie électronique.",
        "Conséquence directe : ce taux forfaitaire sous-prélève souvent, parfois de plusieurs milliers d'euros par an. L'écart apparaît à la déclaration commune, qui recalcule l'impôt réellement dû sur le revenu cumulé.",
        { h: "Ne pas tout laisser à la régularisation" },
        "Deux mécanismes évitent d'attendre la déclaration. Si le prélèvement est trop élevé, vous pouvez demander un taux réduit : la demande se fait par écrit, en joignant les fiches de salaire des trois derniers mois des deux conjoints. Si au contraire il est insuffisant, sachez qu'un taux plus élevé ne peut pas être inscrit sur une fiche additionnelle : le complément prend alors la forme d'avances trimestrielles fixées par l'administration.",
        "Autrement dit, la première année se solde généralement en une fois, puis le système se lisse par des avances. Anticipez ce solde initial, c'est la mauvaise surprise la plus courante des ménages à deux revenus.",
        { h: "Le cas fréquent du conjoint non résident" },
        "Un couple dont l'un des membres réside encore à l'étranger relève par défaut de la classe 1, nettement moins favorable. L'option pour l'imposition collective existe et mérite d'être étudiée chiffres en main : selon les revenus respectifs, elle peut représenter plusieurs milliers d'euros par an, dans un sens comme dans l'autre.",
        { h: "Deux mécanismes qui surprennent" },
        "À l'impôt calculé s'ajoute une contribution au fonds pour l'emploi de 7 %, portée à 9 % au-delà de 150 000 euros de revenu imposable en classe 1 et 1a, ou 300 000 euros en classe 2. Par ailleurs, les revenus étrangers exonérés au Luxembourg sont malgré tout retenus pour déterminer le taux appliqué aux revenus luxembourgeois : c'est le taux moyen mondial, qui explique souvent l'écart entre la simulation et la première déclaration.",
        "Réclamez enfin votre fiche de retenue d'impôt dès l'arrivée, et vérifiez que l'employeur l'a bien reçue. Sans ce document, la retenue s'effectue au taux maximal, et la régularisation n'interviendra qu'à la déclaration suivante."
      ],
      aRetenir: [
        "La classe 2 change tout à haut revenu.",
        "Réclamer la fiche de retenue dès l'arrivée.",
        "Les revenus étrangers exonérés influencent quand même le taux."
      ],
      sources: [{ t: "Administration des contributions directes, barèmes", u: "https://impotsdirects.public.lu/fr/baremes.html" }]
    },
    {
      id: "deductions",
      titre: "Ce qui réduit votre impôt",
      cat: "Impots",
      resume: "Au-delà de la classe, plusieurs postes viennent en déduction : trajets, prévoyance, épargne-logement, garde d'enfants.",
      tags: ["déduction", "dépenses spéciales", "frais de déplacement", "épargne-logement", "prévoyance", "assurance", "impôt", "déclaration"],
      corps: [
        "Le net perçu chaque mois ne dit pas tout : une part de l'impôt se régularise à la déclaration, et plusieurs postes viennent en déduction. Les connaître change le résultat de plusieurs milliers d'euros pour un ménage.",
        { h: "Les forfaits appliqués d'office" },
        "Un forfait de frais d'obtention et un forfait de dépenses spéciales sont retenus automatiquement, sans justificatif. Ils constituent un plancher : si vos dépenses réelles les dépassent, vous avez intérêt à les déclarer au réel.",
        { h: "Les principaux postes déductibles" },
        "Les frais de déplacement entre domicile et lieu de travail sont déductibles selon un barème forfaitaire fondé sur la distance, ce qui concerne particulièrement ceux qui résident loin de leur employeur. Les intérêts d'un prêt pour l'habitation principale font l'objet d'un régime propre, détaillé dans une fiche dédiée.",
        "Viennent ensuite les cotisations de prévoyance vieillesse individuelle, les primes d'assurance vie et de responsabilité civile, les cotisations d'épargne-logement, et, pour les ménages avec enfants, les frais de garde. Chacun de ces postes obéit à son propre plafond, qui dépend parfois de la composition du foyer.",
        { h: "Les rentes versées" },
        "Une pension alimentaire versée à un ex-conjoint peut être déduite sous conditions, y compris lorsque le bénéficiaire réside à l'étranger. C'est un poste fréquemment oublié par les nouveaux arrivants, et souvent significatif.",
        "Les montants et plafonds de ces dispositifs évoluent régulièrement. Le simulateur de ce guide se concentre volontairement sur la retenue à la source, qui dépend de la classe et du barème ; pour l'optimisation de la déclaration, appuyez-vous sur les fiches officielles de l'administration, ou faites-vous accompagner la première année."
      ],
      aRetenir: [
        "Les forfaits sont un plancher, pas un plafond.",
        "Déplacements, prévoyance, épargne-logement et garde d'enfants sont déductibles.",
        "Se faire accompagner pour la première déclaration."
      ],
      sources: [{ t: "ACD, index des matières", u: "https://impotsdirects.public.lu/fr/az.html" }]
    },
    {
      id: "conseil_fiscal",
      titre: "Conseil fiscal : les leviers de la première année",
      cat: "Impots",
      resume: "Vérifier la classe d'impôt et la faire corriger, puis activer les déductions qui rapportent : 111bis, épargne-logement.",
      tags: ["conseil fiscal", "111bis", "prévoyance-vieillesse", "épargne-logement", "classe", "fiche de retenue", "rectification", "correction", "164 R", "bureau RTS", "optimisation", "déduction"],
      corps: [
        "La première année, quelques décisions simples pèsent plus sur l'impôt final que tout le reste. Cette fiche regroupe les leviers concrets, dans l'ordre où ils se présentent.",
        { h: "D'abord, vérifier la classe d'impôt, et la faire corriger si elle est fausse" },
        "La retenue mensuelle repose sur la fiche de retenue d'impôt. Pour un salarié qui commence à travailler au Luxembourg, elle est émise automatiquement par l'administration après l'affiliation au Centre commun de la sécurité sociale, faite par l'employeur, sous une trentaine de jours ouvrables : il n'y a pas de démarche à faire pour l'obtenir. La démarche, c'est de la vérifier. Pour un nouvel arrivant, la situation familiale est parfois mal reprise et la classe attribuée est alors erronée, ce qui arrive plus souvent qu'on ne le croit. Ni vous ni votre employeur n'avez le droit de corriger la fiche vous-mêmes.",
        "La modification se demande sans tarder au bureau RTS compétent pour votre commune, de préférence avec le formulaire 164 R. Tant que la fiche n'est pas corrigée, la retenue se fait sur la mauvaise classe ; l'écart se régularise ensuite, mais autant ne pas avancer cet argent des mois durant.",
        { h: "Le contrat de prévoyance-vieillesse, dit 111bis" },
        "Les primes versées à un contrat de prévoyance-vieillesse se déduisent jusqu'à 3 200 euros par an et par contribuable, quel que soit l'âge, et chaque conjoint qui souscrit son propre contrat a son propre plafond. En contrepartie, c'est une épargne longue : contrat d'au moins dix ans, épargne remboursable au plus tôt à 60 ans et au plus tard à 75 ans, en capital, en rente viagère ou en mixte. Le remboursement anticipé est exclu, sauf maladie grave ou invalidité.",
        { h: "L'épargne-logement" },
        "Les cotisations à un contrat d'épargne-logement se déduisent jusqu'à 1 344 euros par an et par personne du ménage lorsque le souscripteur a entre 18 et 41 ans, 672 euros au-delà. Le plafond se majore pour le conjoint imposé collectivement et pour chaque enfant donnant droit à une modération d'impôt : pour une famille, cela se multiplie vite. L'épargne doit financer l'habitation personnelle ; utilisée à autre chose avant dix ans, la déduction est reprise par imposition rectificative.",
        { h: "Les autres leviers, déjà détaillés dans le guide" },
        "Les frais réels au-delà des forfaits, les primes d'assurance et les frais de garde sont détaillés dans la fiche « Ce qui réduit votre impôt ». Les intérêts d'un prêt pour l'habitation principale ont leur fiche propre, et le régime des impatriés la sienne. Pour un couple dont l'un travaille hors du Luxembourg, le choix d'imposition en classe 2 mérite un calcul, voir la fiche sur les classes d'impôt.",
        "Les plafonds cités ici sont ceux publiés par Guichet.lu à la date de vérification de ce guide. Ils évoluent : avant de signer un contrat pour raison fiscale, la page officielle fait foi."
      ],
      aRetenir: [
        "Classe erronée : demander la correction au bureau RTS, formulaire 164 R, sans tarder.",
        "111bis : jusqu'à 3 200 € par an et par contribuable, épargne bloquée jusqu'à 60 ans au plus tôt.",
        "Épargne-logement : 1 344 € par personne jusqu'à 41 ans, 672 € ensuite, plafond multiplié par la taille du ménage."
      ],
      sources: [
        { t: "Guichet.lu, faire modifier la fiche de retenue d'impôt", u: "https://guichet.public.lu/fr/citoyens/fiscalite/declaration-impot-decompte/fiche-retenue-impot/demander-modifier-fiche-impot.html" },
        { t: "Guichet.lu, contrat de prévoyance-vieillesse", u: "https://guichet.public.lu/fr/citoyens/fiscalite/declaration-impot-decompte/depenses-deductibles/contrat-prevoyance-resident.html" },
        { t: "Guichet.lu, cotisations d'épargne-logement", u: "https://guichet.public.lu/fr/citoyens/fiscalite/immobilier/depenses-deductibles/epargne-logement-resident.html" }
      ]
    },
    {
      id: "impatries",
      titre: "Régime des impatriés",
      cat: "Impots",
      resume: "Réservé à des profils précis : la moitié de la rémunération brute exonérée, pendant neuf années au maximum.",
      tags: ["impatrié", "expatrié", "exonération", "régime", "prime", "avantage fiscal"],
      corps: [
        "Ce régime ne concerne qu'une minorité de nouveaux arrivants, mais son effet est considérable pour ceux qui y ont droit. Depuis le 1er janvier 2025, il exonère d'impôt 50 % de la rémunération brute annuelle totale, dans la limite de 400 000 euros de rémunération éligible. La différence avec l'ancien dispositif est importante : il ne s'agit plus d'une prime distincte, mais d'une exonération portant sur l'ensemble de la rémunération.",
        { h: "Qui peut en bénéficier" },
        "Le régime s'adresse aux personnes réellement venues de l'étranger pour occuper le poste. Il faut n'avoir été ni résident fiscal luxembourgeois, ni avoir exercé une activité au Luxembourg, ni avoir résidé à moins de cent cinquante kilomètres de la frontière, pendant les cinq années précédentes. Cette dernière condition exclut de fait une grande partie des candidats venus de Lorraine, de Wallonie ou de Sarre.",
        "S'y ajoute une condition de rémunération, avec un brut annuel fixe minimum de l'ordre de 75 000 euros hors avantages en nature. Côté employeur, le nombre d'impatriés ne doit pas dépasser 30 % de l'effectif, avec des assouplissements pour les entreprises récentes.",
        { h: "Deux effets que l'on oublie souvent" },
        "Premier effet : l'exonération augmente mécaniquement votre revenu net, ce qui peut vous faire dépasser des plafonds utilisés ailleurs, notamment pour l'accès au logement abordable ou à certaines aides. Un avantage fiscal peut donc fermer une autre porte.",
        "Second effet, plus structurant : le régime s'éteint. Il court sur l'année du début d'activité au Luxembourg, plus les huit suivantes, soit neuf années d'imposition au maximum. Un prêt immobilier sur vingt-cinq ou trente ans doit donc être calibré sur le revenu d'après le régime, pas sur celui d'aujourd'hui. C'est l'erreur la plus coûteuse que commettent les nouveaux arrivants bien rémunérés.",
        "Enfin, le bénéfice n'est pas automatique : il se demande via l'employeur, qui doit respecter des obligations déclaratives. Vérifiez son application sur vos premières fiches de paie."
      ],
      aRetenir: [
        "Conditions strictes, dont l'éloignement de la frontière les cinq années précédentes.",
        "Neuf années d'imposition au maximum, puis extinction.",
        "Bâtir tout crédit long sur le revenu d'après le régime."
      ],
      sources: [{ t: "Administration des contributions directes", u: "https://impotsdirects.public.lu/fr/az.html" }]
    },
    {
      id: "interets",
      titre: "Déduire les intérêts d'emprunt",
      cat: "Impots",
      resume: "Le plafond se multiplie par le nombre de personnes du ménage, ce qui change tout pour une famille.",
      tags: ["intérêts", "déduction", "emprunt", "crédit", "habitation", "plafond", "résidence principale", "impôt"],
      corps: [
        "Les intérêts d'un prêt finançant le logement que vous occupez sont déductibles, dans la catégorie des revenus provenant de la location de biens. Depuis l'année d'imposition 2024, le plafond annuel dépend de l'ancienneté de l'occupation : la déduction est intégrale l'année de fixation de la valeur locative et l'année suivante, puis limitée à 4 000 euros pendant quatre ans, à 3 000 euros pendant les cinq années suivantes, et à 2 000 euros ensuite.",
        { h: "L'élément décisif : la composition du ménage" },
        "Ces montants sont souvent cités seuls, ce qui donne une image fausse du dispositif. Le plafond est en réalité majoré de son propre montant pour le conjoint ou partenaire imposé collectivement, ainsi que pour chaque enfant ouvrant droit à une modération d'impôt. Pour un couple avec deux enfants, le plafond effectif est donc quadruplé : 16 000 euros par an, puis 12 000, puis 8 000.",
        "Sur un prêt de 800 000 euros à 4,3 %, les intérêts de la première année avoisinent 34 000 euros. Les deux premières années passent donc intégralement, mais dès la troisième, le plafond mord sur près de la moitié des intérêts. Le calcul mérite d'être fait avant de s'engager.",
        "Deux limites enfin : les subventions d'intérêts versées par l'État réduisent d'autant la dépense déductible, et les intérêts liés à une résidence secondaire ne sont pas déductibles."
      ],
      tableaux: [{
        titre: "Plafond annuel selon la composition du ménage",
        colonnes: ["Période", "1 personne", "Couple", "Couple et 2 enfants"],
        lignes: [
          ["Années 1 et 2", "intégral", "intégral", "intégral"],
          ["Années 3 à 6", "4 000 €", "8 000 €", "16 000 €"],
          ["Années 7 à 11", "3 000 €", "6 000 €", "12 000 €"],
          ["Ensuite", "2 000 €", "4 000 €", "8 000 €"]
        ]
      }],
      aRetenir: [
        "Le plafond se multiplie par le nombre de membres du ménage.",
        "Les deux premières années sont sans plafond.",
        "Résidence principale uniquement."
      ],
      sources: [{ t: "ACD, habitation personnelle du propriétaire", u: "https://impotsdirects.public.lu/fr/az/h/habit_pers.html" }]
    },
    {
      id: "independant",
      titre: "Exercer en indépendant",
      cat: "Travail",
      resume: "Déclaration au CCSS sous huit jours, dispense possible, et une autorisation d'établissement à vérifier avant de facturer.",
      tags: ["indépendant", "freelance", "ccss", "affiliation", "dispense", "accessoire", "autorisation", "entreprise"],
      corps: [
        "Le démarrage d'une activité indépendante se déclare au Centre commun de la sécurité sociale dans les huit jours, au moyen de la déclaration d'entrée pour travailleurs indépendants. Exercée à côté d'un emploi salarié, l'activité est dite accessoire : les cotisations sont alors calculées sur le revenu réel, mais avec un plancher fixé au tiers du salaire social minimum.",
        { h: "La dispense pour revenu insignifiant" },
        "Si le revenu tiré de l'activité ne dépasse pas ce tiers sur l'année, vous pouvez demander une dispense d'affiliation. Deux points méritent attention. D'abord, elle ne s'applique jamais d'office : elle se demande expressément, sur un formulaire distinct de la déclaration d'entrée. Ensuite, elle a une contrepartie écrite noir sur blanc : la personne dispensée n'est pas couverte contre les risques maladie, pension, accident et dépendance au titre de cette activité. Sans conséquence si vous êtes par ailleurs salarié, mais à mesurer sinon.",
        "Notez également qu'une dispense d'affiliation ne produit pas de numéro d'affiliation. Si un client exige ce numéro pour vous payer, la dispense n'est pas la bonne option. Enfin, si l'administration fiscale établit ensuite un revenu supérieur au seuil, l'affiliation devient obligatoire et rétroactive.",
        { h: "L'autorisation d'établissement, l'oubli classique" },
        "Beaucoup d'activités, dont le conseil et la formation dispensée à des tiers, exigent une autorisation d'établissement préalable. Facturer sans elle expose à des sanctions. Vérifiez ce point avant d'émettre la moindre facture, et non après : c'est le blocage le plus fréquent, et le plus long à régulariser."
      ],
      aRetenir: [
        "Huit jours après le début de l'activité.",
        "La dispense se demande sur un second formulaire.",
        "Vérifier l'autorisation d'établissement avant de facturer."
      ],
      sources: [{ t: "CCSS, s'affilier comme indépendant", u: "https://ccss.public.lu/fr/independants/commencer-arreter-activite/affilier.html" }]
    },
    {
      id: "allocations",
      titre: "Allocations familiales",
      cat: "Famille",
      resume: "Versées sans condition de nationalité, avec un supplément selon l'âge et une allocation de rentrée automatique.",
      tags: ["allocation", "familiale", "cae", "enfant", "rentrée scolaire", "aide", "prestation", "naissance"],
      corps: [
        "Les prestations familiales sont versées par la Caisse pour l'avenir des enfants, et elles sont dues pour tout enfant résidant au Luxembourg, sans condition de nationalité. C'est la résidence qui compte, pas le passeport, ce qui en fait l'une des aides les plus simples à obtenir pour un nouvel arrivant.",
        "Le montant de base s'élève à 315,04 euros par mois et par enfant, majoré de 23,81 euros au-delà de six ans et de 59,44 euros au-delà de douze ans, valeurs applicables au 1er juin 2026. S'y ajoute chaque année, en août, une allocation de rentrée scolaire de 115 euros pour un enfant de plus de six ans et de 235 euros au-delà de douze ans, versée automatiquement, sans aucune demande.",
        "Seule la demande initiale nécessite une démarche, auprès de la caisse, en ligne via MyGuichet ou par formulaire. Ces montants suivent l'indexation générale : ils sont revalorisés à chaque tranche indiciaire, ce qui explique qu'un montant lu il y a un an ne corresponde plus."
      ],
      tableaux: [{
        titre: "Montants mensuels par enfant au 1er juin 2026",
        colonnes: ["Situation", "Montant"],
        lignes: [
          ["Montant de base", "315,04 €"],
          ["Supplément au-delà de 6 ans", "+ 23,81 €"],
          ["Supplément au-delà de 12 ans", "+ 59,44 €"],
          ["Rentrée scolaire, plus de 6 ans", "115 € en août"],
          ["Rentrée scolaire, plus de 12 ans", "235 € en août"]
        ]
      }],
      aRetenir: [
        "Aucune condition de nationalité, seule la résidence compte.",
        "L'allocation de rentrée est automatique.",
        "Montants revalorisés à chaque tranche indiciaire."
      ],
      sources: [{ t: "Caisse pour l'avenir des enfants", u: "https://cae.public.lu/fr/allocations.html" }]
    },
    {
      id: "conges",
      titre: "Congés et jours fériés",
      cat: "Travail",
      resume: "Vingt-six jours ouvrables de congé légal et onze jours fériés, souvent améliorés par convention collective.",
      tags: ["congé", "vacances", "jours fériés", "travail", "période d'essai", "préavis", "contrat", "salarié"],
      corps: [
        "Le congé annuel légal s'élève à vingt-six jours ouvrables par an. Le droit s'ouvre après trois mois de travail ininterrompu chez le même employeur, puis s'acquiert par douzièmes. Une semaine de congé compte au maximum pour cinq jours ouvrables, même si votre temps de travail est réparti sur davantage de jours. S'y ajoutent onze jours fériés légaux, que l'employeur peut partiellement remplacer par des fêtes locales ou professionnelles, à condition d'en respecter le total.",
        "Ces chiffres ne sont qu'un plancher. Les conventions collectives, très présentes dans la finance et les services, accordent fréquemment plusieurs jours supplémentaires. Avant de comparer deux offres d'emploi, vérifiez la convention applicable : l'écart peut atteindre une semaine entière.",
        { h: "Essai et préavis" },
        "La période d'essai doit être stipulée par écrit au plus tard au moment de l'entrée en service, et sa durée conditionne le préavis applicable pendant cette période. À la fin du contrat, retenez un point souvent ignoré : le congé reste dû pendant le préavis, même en cas de dispense de travail, et l'employeur ne peut pas vous imposer de solder vos jours à ce moment-là."
      ],
      aRetenir: [
        "Vingt-six jours ouvrables et onze jours fériés au minimum.",
        "Droit ouvert après trois mois chez le même employeur.",
        "Vérifier la convention collective avant de comparer deux offres."
      ],
      sources: [
        { t: "Inspection du travail et des mines", u: "https://itm.public.lu/fr/conditions-travail/conges.html" },
        { t: "Guichet.lu, congé légal annuel", u: "https://guichet.public.lu/fr/citoyens/travail/conges-jours-feries/annuel/conge-legal-annuel.html" }
      ]
    },
    {
      id: "emploi",
      titre: "Chercher un emploi",
      cat: "Travail",
      resume: "Un marché très international, où les postes vacants doivent être déclarés au service public de l'emploi.",
      tags: ["emploi", "travail", "adem", "chômage", "recrutement", "cv", "candidature", "frontalier"],
      corps: [
        "L'Agence pour le développement de l'emploi est le service public de l'emploi. S'y inscrire ouvre l'accès à un accompagnement, à des formations et, sous conditions, à l'indemnisation. Un détail vaut à lui seul l'inscription : les employeurs sont légalement tenus de lui déclarer leurs postes vacants, ce qui en fait une source d'offres qui ne circulent pas toujours ailleurs.",
        "Le marché est profondément international. Une part considérable des salariés vient chaque jour de France, de Belgique et d'Allemagne, ce qui élargit d'autant la concurrence sur chaque poste, mais explique aussi que le multilinguisme soit un critère de sélection à part entière. Les secteurs les plus actifs restent la finance, les technologies, la logistique, la santé et les institutions européennes.",
        "Côté candidature, la présentation locale est sobre, en français ou en anglais selon l'employeur. Mentionnez explicitement votre niveau dans chaque langue, plutôt qu'une liste sans précision : c'est l'information que le recruteur cherche en premier, et son absence est souvent lue comme un aveu."
      ],
      aRetenir: [
        "L'inscription à l'ADEM ouvre des offres non publiées ailleurs.",
        "Préciser le niveau atteint dans chaque langue.",
        "Concurrence élargie aux régions frontalières."
      ],
      sources: [{ t: "ADEM", u: "https://adem.public.lu/fr.html" }]
    },
    {
      id: "banque",
      titre: "Banque et moyens de paiement",
      cat: "Finances",
      resume: "Un compte local n'est pas obligatoire, mais il débloque la garantie locative et l'identité numérique.",
      tags: ["banque", "compte", "iban", "garantie", "virement", "sepa", "frais", "ouverture"],
      corps: [
        "Un IBAN européen suffit légalement pour recevoir un salaire et payer un loyer : refuser un compte d'un autre pays de la zone SEPA est interdit. En pratique, deux obstacles justifient malgré tout l'ouverture d'un compte luxembourgeois. Le premier est la garantie locative, que les banques étrangères n'émettent généralement pas. Le second est LuxTrust, souvent offert aux clients d'une banque locale et payant en direct.",
        "L'ouverture demande une pièce d'identité, un justificatif d'adresse et, le plus souvent, le contrat de travail. Les principaux acteurs de détail sont Spuerkeess, la banque de l'État, ainsi que BIL, BGL BNP Paribas, ING et la Banque Raiffeisen.",
        "Comparez les grilles tarifaires avant de choisir, car elles varient nettement d'un établissement à l'autre. Regardez non seulement les frais de tenue de compte, mais aussi le coût des cartes, celui des virements et les conditions d'exonération liées à la domiciliation du salaire."
      ],
      aRetenir: [
        "Un IBAN SEPA étranger ne peut pas être refusé pour le salaire.",
        "Compte local nécessaire en pratique pour la garantie locative.",
        "Ouvrir le compte avant de signer un bail."
      ],
      sources: [{ t: "luxembourg.public.lu, vivre au Luxembourg", u: "https://luxembourg.public.lu/fr/vivre.html" }]
    },
    {
      id: "cout_vie",
      titre: "Salaire, coût de la vie et indexation",
      cat: "Finances",
      resume: "Des salaires élevés, un logement cher, et une indexation automatique qui n'existe pas ailleurs.",
      tags: ["salaire", "coût de la vie", "index", "indexation", "ssm", "minimum", "tranche indiciaire", "treizième mois"],
      corps: [
        "Le Luxembourg applique une indexation automatique des salaires : dès que l'indice des prix progresse de 2,5 %, une tranche indiciaire est déclenchée et l'ensemble des salaires, pensions et minima sociaux est revalorisé d'autant. Ce mécanisme, rare en Europe, explique que les montants légaux changent plusieurs fois sur une même période et qu'une valeur trouvée en ligne soit vite obsolète.",
        "Le salaire social minimum figure parmi les plus élevés d'Europe et sert de référence à de nombreux plafonds sociaux et fiscaux, à commencer par le plafond cotisable, fixé à cinq fois son montant.",
        { h: "Où part l'avantage salarial" },
        "Le logement absorbe l'essentiel de l'écart de rémunération avec les régions voisines, ce qui explique qu'une part importante des salariés choisisse de résider de l'autre côté de la frontière. En contrepartie, les transports publics sont gratuits et la fiscalité des ménages, en particulier en classe 2, se révèle souvent plus favorable que chez les voisins.",
        "Un dernier point à vérifier avant de signer : le treizième mois est fréquent mais n'a rien d'une obligation légale. Il dépend du contrat ou de la convention collective. Comparez donc les offres sur le brut annuel total, et non sur le salaire mensuel affiché."
      ],
      aRetenir: [
        "Indexation automatique à chaque 2,5 % d'inflation.",
        "Plafond cotisable : cinq fois le salaire social minimum.",
        "Comparer les offres sur le brut annuel, treizième mois compris."
      ],
      sources: [{ t: "STATEC", u: "https://statistiques.public.lu/fr.html" }]
    },
    {
      id: "ecole",
      titre: "Scolariser ses enfants",
      cat: "Famille",
      resume: "Trois voies possibles, et un choix qui se prépare bien avant le déménagement.",
      tags: ["école", "enfant", "scolarité", "européenne", "internationale", "publique", "inscription", "rentrée", "langue"],
      corps: [
        "C'est la démarche la plus longue et la plus contrainte de toute l'installation, et celle qu'il faut engager en premier, avant même la recherche de logement. Trois voies coexistent, très différentes.",
        { h: "L'école publique, gratuite et multilingue" },
        "L'enseignement public luxembourgeois alphabétise en luxembourgeois, puis introduit l'allemand, puis le français. Il intègre remarquablement bien les enfants arrivés jeunes. Pour une arrivée plus tardive, en revanche, la barrière linguistique est réelle et doit être évaluée sans complaisance, en fonction de l'âge et du parcours de l'enfant.",
        { h: "Les écoles européennes et internationales" },
        "Les écoles européennes proposent un cursus par section linguistique menant au baccalauréat européen. L'accès est prioritaire pour les enfants de personnels des institutions européennes ; les autres familles sont admises selon les places disponibles et acquittent un minerval. Il existe par ailleurs des écoles internationales publiques, gratuites, avec des sections anglophone, francophone ou germanophone, très demandées. Enfin, des établissements privés proposent le baccalauréat international ou des cursus étrangers, avec des frais significatifs.",
        { h: "Anticiper, et emporter les bons papiers" },
        "L'inscription à la commune précède généralement l'inscription scolaire, et les places dans les sections les plus recherchées partent parfois un an à l'avance. Vérifiez la disponibilité de la section linguistique visée, et pas seulement celle de l'établissement. Avant de quitter votre pays, demandez à l'école d'origine un certificat de radiation ainsi que les bulletins des dernières années : ils sont presque toujours exigés, et bien plus difficiles à obtenir une fois parti. La rentrée se situe au début du mois de septembre."
      ],
      aRetenir: [
        "Commencer par l'école, avant même le logement.",
        "Vérifier la section linguistique, pas seulement l'établissement.",
        "Récupérer certificat de radiation et bulletins avant de partir."
      ],
      sources: [
        { t: "Ministère de l'Éducation nationale", u: "https://men.public.lu/fr.html" },
        { t: "Écoles européennes", u: "https://www.eursc.eu/fr" }
      ]
    },
    {
      id: "garde",
      titre: "Garde d'enfants et chèque-service",
      cat: "Famille",
      resume: "Une aide de l'État qui réduit fortement le coût de la garde, à condition que la structure soit conventionnée.",
      tags: ["crèche", "garde", "chèque-service", "csa", "maison relais", "foyer de jour", "enfant"],
      corps: [
        "Le chèque-service accueil subventionne les heures de garde chez les prestataires conventionnés. Il s'obtient à la commune de résidence, sur présentation du certificat de résidence et des pièces d'identité, ce qui suppose d'avoir déjà effectué la déclaration d'arrivée. Le tarif horaire restant à votre charge dépend du revenu du ménage et du rang de l'enfant dans la fratrie, et certaines heures d'encadrement ainsi que des repas sont pris en charge dans des limites définies.",
        "Un point de vigilance domine tous les autres : vérifiez que la structure est bien conventionnée. Toutes les crèches privées ne le sont pas, et l'écart de coût entre une place conventionnée et une place qui ne l'est pas se chiffre en centaines d'euros par mois.",
        "Enfin, inscrivez-vous sur les listes d'attente très tôt, sans attendre d'avoir un logement définitif ni une date d'arrivée certaine. Dans les communes les plus demandées, les délais se comptent en mois, parfois davantage pour les tout-petits."
      ],
      aRetenir: [
        "Passer par la commune, après l'enregistrement.",
        "Vérifier le conventionnement avant de signer.",
        "S'inscrire sur les listes d'attente le plus tôt possible."
      ],
      sources: [{ t: "Guichet.lu, famille et éducation", u: "https://guichet.public.lu/fr/citoyens/famille-education.html" }]
    },
    {
      id: "sante_pratique",
      titre: "Se soigner au quotidien",
      cat: "Sante",
      resume: "Libre choix du médecin, aucun parcours de soins imposé, et un numéro unique pour les urgences.",
      tags: ["médecin", "urgence", "112", "pharmacie", "hôpital", "garde", "santé", "soins", "spécialiste"],
      corps: [
        "Le numéro d'urgence est le 112, gratuit et valable dans tout le pays depuis n'importe quel téléphone. Pour les soins courants, le système diffère nettement du modèle français : il n'existe ni médecin traitant obligatoire, ni parcours de soins imposé. Vous choisissez librement votre médecin et pouvez consulter un spécialiste directement, sans passer par un généraliste.",
        "Cette liberté a une contrepartie pratique : trouver un médecin qui accepte de nouveaux patients peut prendre du temps à l'arrivée. Lancez les recherches sans attendre d'en avoir besoin, en particulier pour un pédiatre ou un gynécologue.",
        "Pour les soirs et les week-ends, les pharmacies de garde et les maisons médicales de garde assurent la continuité des soins, avec des listes publiées en ligne et mises à jour quotidiennement. Repérez celle qui dessert votre commune dès l'installation."
      ],
      aRetenir: [
        "112 pour les urgences, gratuit.",
        "Aucun parcours de soins imposé, accès direct aux spécialistes.",
        "Chercher un médecin dès l'arrivée, avant d'en avoir besoin."
      ],
      sources: [{ t: "Portail Santé", u: "https://sante.public.lu/fr.html" }]
    },
    {
      id: "permis",
      titre: "Permis de conduire",
      cat: "Mobilite",
      resume: "Un permis européen reste valable sans limite. L'enregistrement est facultatif, gratuit, et ne se fait pas en ligne.",
      tags: ["permis", "conduire", "snca", "échange", "enregistrement", "voiture", "duplicata"],
      corps: [
        "Un permis délivré par un État de l'Espace économique européen reste valable au Luxembourg, sans limitation de durée. Ni l'échange ni l'enregistrement ne sont obligatoires, contrairement à une idée répandue chez les nouveaux arrivants.",
        "L'enregistrement auprès de la Société nationale de circulation automobile présente néanmoins un intérêt concret : en cas de perte ou de vol, l'administration connaît l'existence et les données de votre permis, et peut délivrer rapidement un duplicata. Sans cet enregistrement, il faut repasser par l'autorité qui a délivré le titre, souvent depuis l'étranger, ce qui peut prendre des semaines.",
        "La demande se dépose par courrier postal auprès du service des permis de conduire, ou en personne à l'accueil de Guichet.lu. Elle ne se fait ni par courriel ni via MyGuichet : la seule démarche permis disponible en ligne est la prise de rendez-vous pour l'examen théorique. Joignez le formulaire de transcription, échange ou enregistrement, une photocopie recto verso lisible du permis et une copie d'une pièce d'identité valide. L'enregistrement est gratuit, alors que l'échange contre un permis luxembourgeois est payant."
      ],
      aRetenir: [
        "Permis européen valable sans limite, aucune obligation.",
        "Enregistrement gratuit, par courrier ou au guichet.",
        "Utile surtout pour obtenir vite un duplicata."
      ],
      sources: [{ t: "Guichet.lu, permis étranger", u: "https://guichet.public.lu/fr/citoyens/transport/transports-individuels/permis-conduire/international/transcription-enregistement-permis.html" }]
    },
    {
      id: "vehicule",
      titre: "Immatriculer son véhicule",
      cat: "Mobilite",
      resume: "Trois situations très différentes selon que vous importez, achetez chez un professionnel ou entre particuliers.",
      tags: ["voiture", "véhicule", "immatriculation", "snca", "snct", "contrôle technique", "plaque", "import", "occasion"],
      corps: [
        "Toutes les immatriculations ne se ressemblent pas. Identifiez d'abord votre situation, car la charge de travail et les pièces demandées varient fortement.",
        { h: "Premier cas : vous importez votre véhicule" },
        "Un véhicule venant d'un autre État membre n'est soumis ni aux droits de douane ni à une TVA d'importation, dès lors qu'il n'est pas neuf au sens fiscal. La procédure tient en trois étapes dont l'ordre compte : contrôle technique à la SNCT, souscription d'une assurance luxembourgeoise, puis immatriculation à la SNCA. Réunissez le certificat d'immatriculation d'origine, le certificat de conformité européen, le contrôle technique valide, une pièce d'identité, une preuve de résidence et l'attestation d'assurance. Le certificat d'origine devra ensuite être restitué au pays de départ.",
        { h: "Deuxième cas : vous achetez chez un professionnel" },
        "C'est de loin le plus simple. Le concessionnaire, neuf ou occasion, se charge normalement de l'immatriculation et vous remet le véhicule prêt à rouler. Votre seule obligation reste l'assurance, à souscrire avant la livraison. Vérifiez toutefois ce qui est inclus dans le prix : plaques, taxe, et éventuel contrôle technique.",
        { h: "Troisième cas : vous achetez entre particuliers" },
        "La démarche vous incombe entièrement. Le véhicule doit disposer d'un contrôle technique valide, et le vendeur vous remet les deux parties du certificat d'immatriculation. Rendez-vous ensuite à la SNCA, idéalement accompagné du vendeur, avec la facture ou l'acte de vente. C'est la voie la moins chère, mais celle qui exige le plus de vigilance sur l'état réel du véhicule.",
        { h: "Deux réflexes utiles" },
        "Demandez à votre assureur précédent un relevé d'informations : le bonus se transfère en général, ce qui réduit sensiblement la prime. Et ne laissez pas traîner la démarche, car circuler durablement avec des plaques étrangères tout en résidant au Luxembourg expose à une amende."
      ],
      aRetenir: [
        "Import : contrôle technique, assurance, puis immatriculation.",
        "Achat chez un professionnel : il s'en charge, sauf l'assurance.",
        "Achat entre particuliers : les deux parties du certificat sont indispensables."
      ],
      sources: [{ t: "Guichet.lu, véhicule motorisé", u: "https://guichet.public.lu/fr/citoyens/transport/transports-individuels/vehicule-motorise.html" }]
    },
    {
      id: "assurance_auto",
      titre: "Assurer son véhicule",
      cat: "Mobilite",
      resume: "Obligatoire avant de rouler, et le relevé d'informations de votre ancien assureur vaut de l'argent.",
      tags: ["assurance", "auto", "voiture", "responsabilité civile", "bonus", "malus", "relevé d'informations", "franchise"],
      corps: [
        "L'assurance en responsabilité civile est obligatoire pour tout véhicule immatriculé, et l'attestation est exigée au moment de l'immatriculation. Elle doit donc être souscrite avant, et non après.",
        { h: "Faire valoir son historique" },
        "Le point qui pèse le plus sur la prime est votre historique de sinistres. Demandez à votre assureur précédent un relevé d'informations, document qui récapitule vos années d'assurance et vos éventuels sinistres. La plupart des assureurs luxembourgeois en tiennent compte, ce qui peut représenter plusieurs centaines d'euros par an. Réclamez-le avant de résilier, il devient plus difficile à obtenir ensuite.",
        { h: "Ce qui distingue les contrats" },
        "Au-delà de la responsabilité civile obligatoire, les garanties dites tous risques couvrent aussi les dommages à votre propre véhicule. La comparaison utile porte sur quelques points précis : le montant des franchises, la présence d'une assistance dès le domicile ou seulement à partir d'une certaine distance, le prêt d'un véhicule de remplacement, et la couverture du bris de glace.",
        "Comme pour l'habitation, ces éléments figurent dans les conditions générales et non dans le devis. Réclamez le document d'information standardisé, les conditions générales et les conditions particulières : deux contrats au même prix peuvent différer nettement sur la franchise et sur l'assistance, c'est-à-dire précisément sur ce que vous constaterez le jour du sinistre."
      ],
      aRetenir: [
        "Assurance obligatoire avant l'immatriculation.",
        "Réclamer le relevé d'informations avant de résilier.",
        "Comparer franchises et assistance, pas seulement la prime."
      ],
      sources: [{ t: "Commissariat aux assurances", u: "https://www.caa.lu/fr/accueil" }]
    },
    {
      id: "transport",
      titre: "Transports publics gratuits",
      cat: "Mobilite",
      resume: "Gratuité totale depuis février 2020, mais une desserte très inégale selon les communes.",
      tags: ["transport", "bus", "train", "tram", "gratuit", "mobilité", "frontalier", "parking"],
      corps: [
        "Depuis le 29 février 2020, les transports publics sont gratuits pour tous sur l'ensemble du territoire : bus, trams et trains en seconde classe, sans titre de transport à acheter. La gratuité bénéficie également aux frontaliers, mais uniquement sur la portion luxembourgeoise de leur trajet. Seule la première classe des trains reste payante.",
        "Cette gratuité pèse davantage qu'on ne le croit dans un budget familial, et peut légitimement remettre en cause l'achat d'une seconde voiture. Encore faut-il que la desserte suive : le réseau est dense autour de la capitale et le long des axes ferroviaires, mais nettement plus clairsemé dans les zones rurales, où la voiture reste souvent indispensable.",
        "Avant de choisir une commune, vérifiez donc concrètement les horaires depuis l'arrêt le plus proche, aux heures qui vous concernent, et pas seulement l'existence d'une ligne. Des parkings relais gratuits en périphérie permettent par ailleurs de combiner voiture et train, et d'éviter le stationnement en ville, cher et contraint."
      ],
      aRetenir: [
        "Gratuit partout, sans titre de transport.",
        "Vérifier la desserte réelle avant de choisir une commune.",
        "Peut remettre en cause l'achat d'une seconde voiture."
      ],
      sources: [{ t: "mobiliteit.lu", u: "https://www.mobiliteit.lu/" }]
    },
    {
      id: "telecom",
      titre: "Internet et téléphonie",
      cat: "Quotidien",
      resume: "Des délais très variables selon la saison, une solution provisoire à réclamer, et une médiation gratuite.",
      tags: ["internet", "fibre", "télécom", "mobile", "box", "opérateur", "ilr", "médiation", "litige", "délai"],
      corps: [
        "Les principaux opérateurs fixes sont POST, l'opérateur historique, ainsi qu'Orange, Tango, Eltrona et Luxembourg Online. La fibre couvre les localités principales, mais l'éligibilité se vérifie à l'adresse exacte, et non à la commune : dans l'ancien, le raccordement peut s'arrêter au pied de l'immeuble.",
        { h: "Des délais qui dépendent de la saison" },
        "Comptez deux à trois semaines entre la souscription et l'installation en période normale. Cette moyenne est trompeuse : les mois de forte mobilité, typiquement de juin à septembre, période des déménagements et des rentrées, allongent sensiblement les délais, parfois au-delà d'un mois. Les fins d'année et les périodes de congés produisent le même effet, pour une autre raison : le nombre de techniciens disponibles diminue.",
        "Si votre logement n'est pas encore raccordé, une intervention en deux passages peut être nécessaire, l'une pour amener la fibre, l'autre pour installer l'équipement. Chaque passage suppose votre présence, et un rendez-vous manqué peut être facturé. Faites confirmer par écrit la date et le créneau horaire, et signalez immédiatement toute particularité d'accès, comme un numéro de rue ou un nom d'interphone différent de celui du contrat.",
        { h: "Ne pas rester sans connexion" },
        "Réclamez systématiquement une solution 4G provisoire : plusieurs opérateurs en prêtent une pendant l'attente, mais rarement sans qu'on la demande. C'est le réflexe qui évite de commencer un télétravail sans connexion.",
        { h: "Choisir son offre" },
        "Les offres combinent souvent internet, ligne fixe et télévision, avec un engagement de douze à vingt-quatre mois. Une règle simple mérite d'être retenue : la montée en gamme est possible à tout moment, la descente rarement. Commencez donc petit, vous ajusterez ensuite. Prévoyez une pièce d'identité, le bail ou l'acte de propriété et vos coordonnées bancaires.",
        { h: "En cas de litige" },
        "Le service de médiation de l'Institut luxembourgeois de régulation est compétent pour les litiges avec un opérateur, et sa saisine est gratuite. Une condition est impérative : avoir adressé une réclamation écrite à l'opérateur et n'avoir obtenu aucune réponse, ou une réponse insatisfaisante. Un appel téléphonique, même suivi d'un refus explicite, ne remplit pas cette condition. Écrivez, et conservez la trace."
      ],
      aRetenir: [
        "Deux à trois semaines, mais davantage de juin à septembre.",
        "Réclamer la 4G provisoire, elle n'est pas proposée d'office.",
        "Litige : réclamation écrite d'abord, puis médiation gratuite."
      ],
      sources: [{ t: "ILR, service de médiation", u: "https://www.ilr.lu/mediation/" }]
    },
    {
      id: "langues",
      titre: "Langues et intégration",
      cat: "Quotidien",
      resume: "Trois langues administratives, l'anglais très présent au travail, et des cours subventionnés.",
      tags: ["langue", "luxembourgeois", "allemand", "français", "anglais", "cours", "intégration", "inl"],
      corps: [
        "Le pays fonctionne avec trois langues administratives. Le français domine dans l'administration et les commerces, l'allemand dans la presse et à l'école primaire, le luxembourgeois dans la vie sociale et associative. L'anglais s'est imposé dans les secteurs financier et européen, au point qu'il est possible d'y travailler durablement sans parler les trois autres.",
        "Cette souplesse a toutefois une limite nette : le luxembourgeois devient indispensable pour la naturalisation, ainsi que pour de nombreux emplois publics ou en contact avec le public. Si l'un ou l'autre figure dans vos projets, commencez tôt, car le niveau attendu demande du temps.",
        "L'Institut national des langues propose des cours à tarif réduit dans les quatre langues, avec des sessions du soir adaptées aux salariés. Les communes et le service de la formation des adultes en organisent également, souvent moins chers et plus proches du domicile, avec l'avantage d'y rencontrer d'autres habitants du quartier."
      ],
      aRetenir: [
        "Trois langues administratives, plus l'anglais au travail.",
        "Le luxembourgeois conditionne la naturalisation.",
        "Cours subventionnés à l'INL et dans les communes."
      ],
      sources: [
        { t: "Institut national des langues", u: "https://www.inll.lu/" },
        { t: "Formation des adultes", u: "https://men.public.lu/fr/themes-transversaux/formation-adultes.html" }
      ]
    },
    {
      id: "nationalite",
      titre: "Nationalité luxembourgeoise",
      cat: "Administratif",
      resume: "La double nationalité est admise, mais la naturalisation suppose une résidence durable et une épreuve de langue.",
      tags: ["nationalité", "naturalisation", "citoyenneté", "double nationalité", "luxembourgeois", "résidence"],
      corps: [
        "Le Luxembourg admet la double nationalité : devenir luxembourgeois n'oblige pas à renoncer à sa nationalité d'origine. C'est une différence notable avec plusieurs pays voisins, et elle explique l'intérêt de nombreux résidents de longue durée pour la démarche.",
        "La voie principale est la naturalisation. Elle suppose une résidence régulière d'une durée minimale, la réussite d'une épreuve de luxembourgeois parlé, et la participation à des cours d'instruction civique. D'autres voies existent selon la situation familiale, notamment pour les enfants nés au Luxembourg ou les personnes ayant un ascendant luxembourgeois.",
        "Les conditions exactes, en particulier la durée de résidence exigée et le niveau de langue attendu, ont changé plusieurs fois au cours des dernières années. Vérifiez-les sur la fiche officielle avant d'engager quoi que ce soit, et anticipez surtout le volet linguistique, de loin le plus long à préparer."
      ],
      aRetenir: [
        "Double nationalité admise.",
        "Épreuve de luxembourgeois parlé et instruction civique.",
        "Vérifier la durée de résidence exigée, elle évolue."
      ],
      sources: [{ t: "Guichet.lu, nationalité luxembourgeoise", u: "https://guichet.public.lu/fr/citoyens/citoyennete/nationalite-luxembourgeoise.html" }]
    },
    {
      id: "dechets",
      titre: "Déchets et vie communale",
      cat: "Quotidien",
      resume: "La commune fournit les bacs, publie le calendrier et donne accès au centre de recyclage.",
      tags: ["déchets", "poubelle", "tri", "recyclage", "commune", "collecte", "environnement", "encombrants"],
      corps: [
        "La gestion des déchets relève entièrement de la commune : c'est elle qui fournit les bacs, publie le calendrier de collecte et donne accès au centre de recyclage. Le tri est poussé, avec des jours de collecte distincts pour les ordures ménagères, le papier et le carton, le verre, les emballages, les déchets organiques et les déchets verts.",
        "Les encombrants et les déchets spéciaux se déposent au centre de recyclage, souvent sur présentation d'un justificatif de domicile. Repérez son emplacement et ses horaires dès l'emménagement : c'est précisément le moment où l'on produit le plus de cartons et où l'on se débarrasse de mobilier.",
        "Plus largement, la commune est le guichet de quantité de services du quotidien : certificats, vignettes de stationnement résidentiel, inscriptions scolaires et périscolaires, chèque-service. Récupérer le calendrier de collecte et identifier son interlocuteur communal font partie des gestes qui évitent beaucoup de contrariétés les premières semaines."
      ],
      aRetenir: [
        "Tout passe par la commune.",
        "Repérer le centre de recyclage dès l'emménagement.",
        "Récupérer le calendrier de collecte."
      ],
      sources: [{ t: "Portail de l'environnement", u: "https://environnement.public.lu/fr.html" }]
    }
  ],

  faq: [
    { q: "Comment choisir ma commune ?", a: "Pas sur la distance. En voiture, élargissez la zone et regardez le temps aux heures de pointe. En transports, vérifiez les horaires réels depuis l'arrêt le plus proche, y compris le week-end. Et cherchez autour de l'école, pas autour du bureau.", fiche: "choisir_commune" },
    { q: "Comment éviter qu'une installation échoue ?", a: "Signalez par écrit l'adresse exacte, l'étage et le nom figurant sur l'interphone, qui diffère souvent du nom du contrat. Prévenez qu'un appel masqué ne peut pas être rappelé, et exigez la confirmation écrite du créneau.", fiche: "rdv_technique" },
    { q: "On me facture des frais d'absence alors que j'étais là", a: "Lisez la fiche d'intervention : elle indique souvent que le client n'est pas responsable tout en facturant les frais, ce qui est un argument solide. Vérifiez aussi l'adresse portée sur la fiche et l'heure des appels, puis contestez par écrit.", fiche: "rdv_technique" },
    { q: "Comment contester une facture ?", a: "Par écrit, toujours. Un appel téléphonique ne vaut pas réclamation, et la médiation exige cette réclamation écrite préalable. Appuyez-vous en priorité sur les documents produits par le professionnel lui-même.", fiche: "reclamation" },
    { q: "Le logement est-il livré avec les luminaires ?", a: "Souvent non. L'usage courant au Luxembourg est de remettre le logement avec les fils au plafond et sans point lumineux monté, là où plusieurs pays voisins laissent une douille équipée. Prévoyez les luminaires et leur pose dès le premier jour, et vérifiez le point à l'état des lieux.", fiche: "emmenagement" },
    { q: "Mes meubles vont-ils passer dans l'ascenseur ?", a: "Mesurez la cabine, la porte palière et les paliers avant de commander. Les armoires hautes existent souvent en deux hauteurs, et seule la plus petite passe dans un ascenseur d'immeuble courant.", fiche: "emmenagement" },
    { q: "Ai-je besoin d'une complémentaire santé ?", a: "Elle est facultative et couvre surtout la chambre seule, le dentaire et l'optique. Si vous en prenez une, souscrivez dès l'arrivée : la plupart des contrats prévoient des délais de carence de plusieurs mois sur ces postes.", fiche: "assurance_sante" },
    { q: "Quels documents demander à un assureur ?", a: "Le document d'information standardisé de deux pages, les conditions générales et les conditions particulières. Le devis ne suffit pas : ce sont les exclusions et les franchises qui décident de ce qui sera refusé le jour du sinistre.", fiche: "assurance_habitation" },
    { q: "L'administration est-elle compliquée ?", a: "Plutôt le contraire : circuits courts, documents souvent délivrés au guichet, interlocuteurs joignables. Ce qui rallonge une installation, ce sont les enchaînements de démarches, chacune conditionnant la suivante.", fiche: "administration" },
    { q: "Mon employeur connaît-il le salaire de mon conjoint ?", a: "Non, et il ne peut pas le déduire. Le taux inscrit sur la fiche additionnelle du second salaire ne dépend que de la classe d'impôt : 15 % en classe 2, 21 % en classe 1a, 33 % en classe 1. Il ne contient aucune information sur le revenu du ménage.", fiche: "impots_classes" },
    { q: "Pourquoi ai-je un solde d'impôt à payer après la déclaration ?", a: "Parce que le taux forfaitaire appliqué au second salaire sous-prélève souvent. La déclaration commune recalcule l'impôt réellement dû sur le revenu cumulé. Les années suivantes, l'administration lisse l'écart par des avances trimestrielles.", fiche: "impots_classes" },
    { q: "Dans quel délai dois-je me déclarer à la commune ?", a: "Dans les huit jours suivant l'emménagement. Cette déclaration produit le certificat de résidence et le matricule, indispensables à toute la suite. Prenez rendez-vous en ligne avant même d'avoir les clés.", fiche: "arrivee" },
    { q: "Comment obtenir mon matricule ?", a: "Il est attribué lors de l'enregistrement à la commune et figure sur le certificat de résidence, sous la mention numéro d'identification. Il compte treize chiffres : votre date de naissance inversée, suivie de cinq chiffres.", fiche: "arrivee" },
    { q: "Ai-je besoin d'un compte bancaire luxembourgeois ?", a: "Pas légalement : un IBAN européen suffit pour le salaire et le loyer. En pratique, un compte local reste nécessaire pour émettre une garantie locative, et il rend LuxTrust souvent gratuit.", fiche: "banque" },
    { q: "Quelle caution peut me demander un bailleur ?", a: "Deux mois de loyer au maximum depuis la réforme du 1er août 2024, contre trois auparavant. Les frais d'agence sont par ailleurs partagés par moitié entre bailleur et locataire.", fiche: "bail" },
    { q: "Comment trouver un logement dans un marché aussi tendu ?", a: "En préparant le dossier complet avant la première visite, et surtout en ayant déjà la capacité d'émettre une garantie bancaire. C'est ce point, plus que le loyer proposé, qui départage les candidats.", fiche: "recherche_logement" },
    { q: "Quand dois-je souscrire l'assurance habitation ?", a: "Avant la remise des clés : l'attestation fait partie du dossier de signature. Comparez sur les franchises, la valeur à neuf et les exclusions, en exigeant les conditions générales et pas seulement le devis.", fiche: "assurance_habitation" },
    { q: "Puis-je conduire avec mon permis européen ?", a: "Oui, sans limite de durée et sans obligation d'échange ni d'enregistrement. L'enregistrement reste utile pour obtenir rapidement un duplicata en cas de perte ou de vol.", fiche: "permis" },
    { q: "Comment immatriculer mon véhicule ?", a: "Cela dépend de votre cas. Import : contrôle technique, assurance, puis immatriculation. Achat chez un professionnel : il s'en charge. Achat entre particuliers : la démarche vous incombe, avec les deux parties du certificat.", fiche: "vehicule" },
    { q: "Les transports publics sont-ils vraiment gratuits ?", a: "Oui, depuis février 2020, sur tout le territoire et pour tous, y compris les frontaliers sur la portion luxembourgeoise. Seule la première classe des trains reste payante.", fiche: "transport" },
    { q: "Qu'est-ce que le régime des impatriés ?", a: "Une exonération portant sur la moitié de la rémunération brute, pendant neuf années au maximum. Attention, les conditions sont strictes : il faut notamment ne pas avoir résidé à moins de 150 km de la frontière durant les cinq années précédentes.", fiche: "impatries" },
    { q: "Quelle différence entre classe 1 et classe 2 ?", a: "La classe 2 applique le splitting aux couples imposés collectivement, ce qui allège fortement l'impôt à revenu égal. À haut revenu, l'écart dépasse souvent mille euros par mois. Le simulateur le chiffre précisément.", fiche: "impots_classes" },
    { q: "Que puis-je déduire de mes impôts ?", a: "Au-delà des forfaits appliqués d'office : les frais de déplacement, les cotisations de prévoyance, les primes d'assurance, l'épargne-logement, les frais de garde et, sous conditions, les pensions alimentaires versées.", fiche: "deductions" },
    { q: "Ma classe d'impôt est fausse, que faire ?", a: "Cela arrive, surtout la première année. Ni vous ni l'employeur ne pouvez corriger la fiche de retenue : demandez la modification au bureau RTS compétent pour votre commune, de préférence avec le formulaire 164 R, sans attendre.", fiche: "conseil_fiscal" },
    { q: "Dois-je demander ma fiche de retenue d'impôt ?", a: "Non, pour un salarié elle est émise automatiquement par l'administration après l'affiliation à la sécurité sociale, faite par l'employeur, sous une trentaine de jours ouvrables. La démarche utile, c'est de vérifier la classe inscrite dès réception.", fiche: "conseil_fiscal" },
    { q: "Puis-je déduire les intérêts de mon prêt immobilier ?", a: "Oui, pour l'habitation principale. La déduction est intégrale les deux premières années, puis plafonnée. Surtout, le plafond est multiplié par le nombre de membres du ménage, conjoint et enfants compris.", fiche: "interets" },
    { q: "Combien puis-je emprunter par rapport au prix du bien ?", a: "Jusqu'à 100 % pour un primo-accédant en résidence principale, 90 % pour un acquéreur déjà propriétaire, et 80 % pour un investissement locatif. Les frais d'acte restent à financer sur fonds propres.", fiche: "achat" },
    { q: "La TVA sur un logement neuf est-elle toujours de 3 % ?", a: "Non. Le taux de 3 % suppose une affectation en habitation principale pendant deux ans et l'accord préalable de l'administration, et l'avantage est plafonné à 50 000 euros par logement. Au-delà, la part restante repasse à 17 %.", fiche: "achat" },
    { q: "Suis-je éligible à un logement à coût modéré ?", a: "Il faut rester sous un plafond de revenu net du ménage et ne posséder aucun autre logement, au Luxembourg comme à l'étranger, au plus tard neuf mois après l'acte. Cette dernière condition vise aussi toutes les personnes vivant avec vous.", fiche: "logement_abordable" },
    { q: "Je veux exercer une activité indépendante en plus de mon emploi", a: "Déclarez-la au CCSS dans les huit jours. Si le revenu reste sous le tiers du salaire social minimum, demandez expressément la dispense pour revenu insignifiant. Vérifiez surtout si votre activité exige une autorisation d'établissement.", fiche: "independant" },
    { q: "Comment inscrire mes enfants à l'école ?", a: "Commencez par là, avant même le logement. L'enregistrement à la commune précède l'inscription scolaire, et les sections les plus demandées se remplissent parfois un an à l'avance.", fiche: "ecole" },
    { q: "Existe-t-il une aide pour la crèche ?", a: "Oui, le chèque-service accueil, obtenu à la commune après l'enregistrement. Il subventionne les heures de garde chez les prestataires conventionnés, avec un reste à charge fonction du revenu.", fiche: "garde" },
    { q: "Quel est le montant des allocations familiales ?", a: "315,04 euros par mois et par enfant, plus 23,81 euros au-delà de six ans et 59,44 euros au-delà de douze ans, montants au 1er juin 2026. Une allocation de rentrée de 115 ou 235 euros est versée automatiquement en août.", fiche: "allocations" },
    { q: "Combien de jours de congés vais-je avoir ?", a: "Vingt-six jours ouvrables de congé légal par an, plus onze jours fériés. Le droit s'ouvre après trois mois chez le même employeur, et les conventions collectives accordent souvent davantage.", fiche: "conges" },
    { q: "Combien de temps pour avoir internet ?", a: "Deux à trois semaines en période normale, mais nettement plus de juin à septembre, saison des déménagements. Réclamez une solution 4G provisoire : plusieurs opérateurs en prêtent une, rarement sans qu'on la demande.", fiche: "telecom" },
    { q: "Que faire en cas de litige avec mon opérateur télécom ?", a: "Adressez d'abord une réclamation écrite à l'opérateur. Sans réponse satisfaisante, saisissez la médiation de l'ILR, gratuite. Attention : un appel téléphonique ne remplace pas la réclamation écrite exigée.", fiche: "telecom" },
    { q: "Dois-je apprendre le luxembourgeois ?", a: "Pas pour travailler : l'anglais et le français suffisent dans beaucoup de secteurs. En revanche, oui pour la naturalisation et pour de nombreux emplois publics. Des cours subventionnés existent.", fiche: "langues" },
    { q: "Quel numéro appeler en cas d'urgence ?", a: "Le 112, gratuit et valable partout. Pour les soins courants, il n'existe ni médecin traitant obligatoire ni parcours de soins : vous consultez directement le spécialiste de votre choix.", fiche: "sante_pratique" },
    { q: "Comment fonctionne le tri des déchets ?", a: "Tout passe par la commune : elle fournit les bacs, publie le calendrier de collecte et donne accès au centre de recyclage. Récupérez ce calendrier dès l'emménagement.", fiche: "dechets" },
    { q: "Peut-on avoir la double nationalité ?", a: "Oui, le Luxembourg l'admet. La naturalisation suppose une résidence durable, une épreuve de luxembourgeois parlé et des cours d'instruction civique. Anticipez le volet linguistique.", fiche: "nationalite" },
    { q: "Qu'est-ce que l'indexation des salaires ?", a: "Un mécanisme automatique : dès que l'indice des prix progresse de 2,5 %, une tranche indiciaire est déclenchée et les salaires, pensions et minima sont revalorisés d'autant.", fiche: "cout_vie" }
  ],

  // Parcours : chaque étape peut renvoyer vers une fiche, et ne s'afficher
  // que si le profil correspond. Conditions possibles :
  //   enfants: true       le foyer a des enfants
  //   vehicule: true      un véhicule accompagne le déménagement
  //   logement: "Louer" | "Acheter"
  //   statut: "Independant" | "Salarie"
  timeline: [
    { phase: "Avant le départ", items: [
      { t: "Contacter les écoles : la démarche la plus longue, à engager avant la recherche de logement.", fiche: "ecole", si: { enfants: true } },
      { t: "Demander à l'école d'origine le certificat de radiation et les bulletins des dernières années.", fiche: "ecole", si: { enfants: true } },
      { t: "S'inscrire sur les listes d'attente des crèches, sans attendre d'avoir un logement.", fiche: "garde", si: { enfants: true } },
      { t: "Vérifier la validité des passeports et pièces d'identité de tout le foyer.", fiche: "arrivee" },
      { t: "Rassembler les actes de naissance et de mariage, récents et traduits si nécessaire.", fiche: "arrivee" },
      { t: "Définir ses critères de commune selon son mode de déplacement et les trajets quotidiens.", fiche: "choisir_commune" },
      { t: "Préparer le dossier de location complet avant la première visite.", fiche: "recherche_logement", si: { logement: "Louer" } },
      { t: "Mesurer la cabine d'ascenseur et les portes avant de commander des meubles.", fiche: "emmenagement" },
      { t: "Étudier les quotités de financement et la règle de TVA avant de faire une offre.", fiche: "achat", si: { logement: "Acheter" } },
      { t: "Demander à son assureur automobile un relevé d'informations, pour transférer le bonus.", fiche: "assurance_auto", si: { vehicule: true } },
      { t: "Vérifier si le régime des impatriés peut s'appliquer, avant la signature du contrat de travail.", fiche: "impatries" }
    ]},
    { phase: "Semaine 1", items: [
      { t: "Déclarer son arrivée à la commune, dans les huit jours.", fiche: "arrivee" },
      { t: "Récupérer le certificat de résidence et noter le matricule.", fiche: "arrivee" },
      { t: "Ouvrir un compte bancaire local et demander LuxTrust dans la foulée.", fiche: "banque" },
      { t: "Souscrire l'assurance habitation, exigée avant la remise des clés.", fiche: "assurance_habitation" },
      { t: "Souscrire internet et réclamer une solution 4G provisoire.", fiche: "telecom" },
      { t: "Pour tout rendez-vous technique, transmettre par écrit l'adresse exacte et le nom sur l'interphone.", fiche: "rdv_technique" },
      { t: "Repérer le calendrier de collecte des déchets et le centre de recyclage.", fiche: "dechets" }
    ]},
    { phase: "Mois 1", items: [
      { t: "Vérifier l'affiliation à la sécurité sociale, faite par l'employeur pour un salarié.", fiche: "matricule" },
      { t: "Attendre la fiche de retenue d'impôt : elle est émise automatiquement après l'affiliation à la sécurité sociale, sous une trentaine de jours ouvrables.", fiche: "impots_classes" },
      { t: "Vérifier la classe inscrite sur la fiche de retenue dès réception, et demander la correction au bureau RTS (formulaire 164 R) si elle est erronée.", fiche: "conseil_fiscal" },
      { t: "Demander les allocations familiales à la Caisse pour l'avenir des enfants.", fiche: "allocations", si: { enfants: true } },
      { t: "Demander le chèque-service accueil à la commune.", fiche: "garde", si: { enfants: true } },
      { t: "Chercher un médecin et un pédiatre, avant d'en avoir besoin.", fiche: "sante_pratique" },
      { t: "Étudier une complémentaire santé, à souscrire tôt à cause des délais de carence.", fiche: "assurance_sante" },
      { t: "Déclarer l'activité indépendante au CCSS, dans les huit jours.", fiche: "independant", si: { statut: "Independant" } }
    ]},
    { phase: "Mois 2 et 3", items: [
      { t: "Assurer le véhicule, préalable obligatoire à l'immatriculation.", fiche: "assurance_auto", si: { vehicule: true } },
      { t: "Immatriculer le véhicule selon votre cas : import, achat professionnel ou entre particuliers.", fiche: "vehicule", si: { vehicule: true } },
      { t: "Enregistrer son permis de conduire, facultatif mais gratuit et utile.", fiche: "permis", si: { vehicule: true } },
      { t: "Vérifier l'application du régime des impatriés sur les fiches de paie.", fiche: "impatries" },
      { t: "Résilier les contrats restés actifs dans le pays de départ.", fiche: "telecom" },
      { t: "Se renseigner sur les cours de langue proposés par la commune.", fiche: "langues" }
    ]},
    { phase: "Première année", items: [
      { t: "Préparer la première déclaration d'impôt et vérifier la classe appliquée.", fiche: "impots_classes" },
      { t: "Recenser les postes déductibles : déplacements, prévoyance, garde, épargne-logement.", fiche: "deductions" },
      { t: "En couple avec un conjoint non résident, étudier l'imposition collective.", fiche: "impots_classes" },
      { t: "Réévaluer les contrats souscrits dans l'urgence : télécoms, assurances, banque.", fiche: "assurance_habitation" },
      { t: "Contester par écrit toute facturation anormale, avant qu'elle ne devienne définitive.", fiche: "reclamation" },
      { t: "Envisager des cours de luxembourgeois si la naturalisation fait partie des projets.", fiche: "nationalite" }
    ]}
  ]
};
