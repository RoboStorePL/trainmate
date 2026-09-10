# TrainMate

A Django community workspace for fitness, rehabilitation, jiu-jitsu and yoga.
The first version includes session booking, trainers, disciplines, account
registration, profile editing, search, pagination and an admin interface.

## Roles and access

- **Client** — can browse and search sessions, book a place, cancel their own
  booking and edit their own profile.
- **Trainer** — has a linked Trainer profile and can create, edit and delete
  only their own training sessions.
- **Admin** — Mixon is the only administrator. The admin has access to the
  **Admin panel** directly from the TrainMate website and can manage clients,
  trainer profiles, disciplines and every training session. The account must be
  created as a Django superuser: `python manage.py createsuperuser --username Mixon`.

## Memberships and trainer payouts

- Clients use the **My membership** page to view their PLN balance, add demo
  funds and choose an active membership plan themselves. This is a learning
  demo: no real card or bank payment is processed.
- An administrator manages the available plans: price, number of sessions and
  validity period. Purchasing a plan automatically activates a membership.
- Booking a session consumes one available membership session; cancellation
  returns it.
- Each trainer has a configurable rate in PLN per participant (default:
  **10.00 PLN**). When a session is marked **Completed**, TrainMate creates a
  salary accrual: `participants × trainer rate`.
- Trainers can review their accruals in **My earnings**. An administrator can
  manually change an accrual amount or mark it Accrued, Ready for payout or
  Paid from **Payouts** or the Django Admin panel.

## Local setup (Python 3.12+)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign up or log in.

```bash
python manage.py test
python manage.py check
```

## Database diagram

```mermaid
erDiagram
    User }o--o{ TrainingSession : participates
    Trainer ||--o{ TrainingSession : teaches
    Specialization ||--o{ TrainingSession : categorizes
    Trainer }o--o{ Specialization : specializes
```

User inherits AbstractUser. Sessions have a future start time, positive
duration/capacity and a coach qualified in the selected discipline.
Booking and cancellation use POST with CSRF protection. Capacity and duplicate
bookings are checked on the server.

This is a shared educational workspace: authenticated members can manage the
catalog. Production role-based permissions are a future step.

AI camera analysis is a future phase and is not implemented in this version.
Screenshots of the final interface should accompany the portfolio pull request.

For deployment set a private SECRET_KEY, DEBUG=0, configure allowed hosts,
static file serving and a production server.
