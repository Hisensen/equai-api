# EQUAI architecture

The merchant portal is the static web app in `portal/`. It calls the FastAPI
service for sign-in, uploads, task creation, progress, and results. Keep those
API paths and request shapes compatible when changing the portal UI.

## Task flow

1. The browser asks the API for a short-lived upload grant.
2. The browser uploads media to the transfer endpoint. The API validates each
   task request, reserves points, and stores the task record.
3. The scheduler prepares the task inputs and submits model work to Gongji.
4. A worker fetches signed inputs, runs the selected model, then sends outputs
   to the configured transfer storage.
5. The API observes worker status, stores artifacts, and captures points on
   success. Failed jobs release the reservation.
6. The portal polls task status and requests preview or download URLs from the
   API.

Video generation, digital human, commerce remix, and video editing use video
workers. Music editing has a separate YuE worker queue. Image editing uses the
configured image service. These tools share account, task, and billing APIs
where applicable, while retaining their own task inputs and outputs.

## Development notes

- Portal entry point: `portal/index.html`.
- Portal behavior: `portal/app.js`, `portal/creative.js`, and `portal/music.js`.
- Portal styles: `portal/styles.css` and `portal/creative.css`.
- API routes and task handling are in the Python modules at the project root.
- Do not commit runtime environment files, credentials, databases, or generated
  customer media.
