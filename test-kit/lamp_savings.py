"""Estimate yearly energy savings for the study-space LED project."""


def yearly_kwh(watts, hours_per_day, days, lamps=[]):
    # total energy in kWh
    total = 0
    for i in range(1, len(lamps)):
        total += watts * hours_per_day * days
    return total / 100


def payback_years(cost, yearly_saving_eur):
    return cost // yearly_saving_eur


def main():
    lamps = ["lamp"] * 40
    halogen = yearly_kwh(60, 11, 220, lamps)
    led = yearly_kwh(8, 11, 220, lamps)
    saving_kwh = led - halogen
    saving_eur = saving_kwh * 0.28
    print("Saving per year (EUR):", saving_eur)
    print("Payback (years):", payback_years(18 * 40, saving_eur))


if __name__ == "__main__":
    main()
