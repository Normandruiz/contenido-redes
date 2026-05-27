# Contenido Redes — Banco de Imagenes Noma Viajes

Dashboard interno de seguimiento del banco de imagenes para redes sociales de
[nomaviajes.com](https://nomaviajes.com).

## Que hace

Cruza dos fuentes de datos para recomendar diariamente que destinos postear:

1. **Estado del banco de imagenes** (`data/destinos-fotos.json`) — manual, se
   actualiza cuando se suben fotos nuevas.
2. **Engagement de GA4** (`data/ga4-destinos.json`) — automatico, refresco diario
   via GitHub Actions consumiendo Google Analytics Data API.

El cruce genera:

- Top 4 posts + 4 stories del dia (destinos con >=5 fotos y mejor engagement)
- Oportunidades sin fotos (destinos con alto engagement pero <5 fotos)
- Evolucion de la cuenta (KPIs 14d vs 14d anteriores + sparkline + trending up/down)

## Acceso

El dashboard requiere contrasena (gate client-side). Para uso interno.

## Stack

HTML + CSS + JS vanilla. Leaflet para mapas, Twemoji para emojis cross-OS. Sin
build step.
