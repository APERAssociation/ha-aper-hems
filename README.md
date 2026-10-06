# APER HEMS pour Home Assistant

Intégration Home Assistant des membres de l'[APER](https://aper-association.ch). Elle envoie chaque quart d'heure
les mesures de votre installation au Cockpit APER HEMS :

- production solaire, import et export réseau, charge et décharge de la batterie, niveau de batterie, consommation ;
- les consommateurs que vous choisissez (pompe à chaleur, chauffe-eau, borne de recharge…).

Les trous (Home Assistant éteint, coupure Internet) sont comblés automatiquement à partir de l'historique de
Home Assistant, jusqu'à 45 jours en arrière.

## Prérequis

- Être membre de l'APER, avec un compte actif sur [aper-association.ch](https://aper-association.ch).
- Une **clé API** personnelle, fournie par l'APER.
- Des capteurs de puissance (W, kW) ou des compteurs d'énergie (Wh, kWh) dans Home Assistant.

## Installation

### Avec HACS

1. HACS → menu ⋮ → **Dépôts personnalisés** → ajoutez `https://github.com/APERAssociation/ha-aper-hems`,
   catégorie **Intégration**.
2. Recherchez **APER HEMS**, installez, puis redémarrez Home Assistant.

### À la main

Copiez le dossier `custom_components/aper_hems` dans le dossier `custom_components` de votre configuration
Home Assistant, puis redémarrez Home Assistant.

## Configuration

1. **Paramètres → Appareils et services → Ajouter une intégration → APER HEMS**.
2. Choisissez votre type d'installation et votre matériel : les capteurs sont détectés automatiquement, vérifiez-les.
3. Collez votre clé API.
4. Pour ajouter des consommateurs : bouton **Configurer** de l'intégration.

Les réglages avancés (compteurs d'énergie, sens des capteurs réseau et batterie) se trouvent dans
**⋮ → Reconfigurer**. Un compteur d'énergie, quand il existe, est plus précis qu'un capteur de puissance.

## Données envoyées

Uniquement les valeurs des capteurs choisis, agrégées par quart d'heure, vers `aper-association.ch`, avec votre clé API.
Aucune autre donnée de Home Assistant n'est transmise.
